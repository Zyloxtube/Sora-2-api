import os
import re
import time
import threading
import uuid
import logging
from flask import Flask, request, jsonify
from pycognito import Cognito
import requests

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Store jobs in memory
jobs = {}

# Configuration
PASSWORD = "Test1234Abc!"
COGNITO_CLIENT_ID = "1kvg8re5bgu9ljqnnkjosu477k"
USER_POOL_ID = "eu-west-1_7hEawdalF"
GUERRILLA_API = "https://api.guerrillamail.com/ajax.php"

# ─── Temp email ──────────────────────────────────────────────────────────────

class TempEmail:
    def __init__(self):
        self.sid_token = None
        self.email_addr = None
        self.seq = 0
        self.seen_ids = set()

    def generate(self):
        r = requests.get(f"{GUERRILLA_API}?f=get_email_address", timeout=15)
        data = r.json()
        self.sid_token = data["sid_token"]
        self.seq = 0
        self.seen_ids = set()
        raw = data["email_addr"]
        at = raw.find("@")
        self.email_addr = (raw[:at + 1] if at != -1 else raw + "@") + "sharklasers.com"
        logger.info(f"Generated email: {self.email_addr}")
        return self.email_addr

    def check_inbox(self):
        if not self.sid_token:
            return None
        try:
            r = requests.get(
                f"{GUERRILLA_API}?f=check_email&sid_token={self.sid_token}&seq={self.seq}",
                timeout=15,
            )
            data = r.json()
            if "seq" in data:
                self.seq = data["seq"]
            for email in data.get("list", []):
                if email["mail_id"] in self.seen_ids:
                    continue
                self.seen_ids.add(email["mail_id"])
                code = self._extract_code(email.get("mail_subject", ""))
                if not code:
                    code = self._fetch_body_code(email["mail_id"])
                if code:
                    logger.info(f"Found verification code: {code}")
                    return code
        except Exception as e:
            logger.error(f"Error checking inbox: {e}")
        return None

    def _fetch_body_code(self, mail_id):
        try:
            r = requests.get(
                f"{GUERRILLA_API}?f=fetch_email&email_id={mail_id}&sid_token={self.sid_token}",
                timeout=15,
            )
            d = r.json()
            body = re.sub(r"<[^>]+>", "", d.get("mail_body", "") or "")
            return (
                self._extract_code(d.get("mail_subject", ""))
                or self._extract_code(body)
            )
        except Exception as e:
            logger.error(f"Error fetching body: {e}")
            return None

    @staticmethod
    def _extract_code(text):
        if not text:
            return None
        m = re.search(r"(\d{6})", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d{5})", text)
        if m:
            return m.group(1)
        m = re.search(r"(\d{4})", text)
        return m.group(1) if m else None

    def wait_for_code(self, timeout=120, interval=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = self.check_inbox()
            if code:
                return code
            time.sleep(interval)
        return None

# ─── Cognito auth ─────────────────────────────────────────────────────────────

def sign_up_with_cognito(email):
    try:
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        cognito.add_custom_attributes({"email": email})
        cognito.register(email, PASSWORD)
        logger.info(f"Signed up: {email}")
        return {"status": "success"}
    except Exception as e:
        error_msg = str(e)
        if "User already exists" in error_msg or "UsernameExistsException" in error_msg:
            logger.info(f"User already exists: {email}")
            return {"status": "exists"}
        raise RuntimeError(f"Sign-up failed: {error_msg}")

def confirm_sign_up_with_cognito(email, code):
    try:
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        cognito.confirm_sign_up(code)
        logger.info(f"Confirmed sign up: {email}")
        return True
    except Exception as e:
        raise RuntimeError(f"Confirmation failed: {str(e)}")

def sign_in_with_cognito(email):
    try:
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        cognito.authenticate(password=PASSWORD)
        id_token = cognito.id_token
        if not id_token:
            raise RuntimeError("Failed to get ID token")
        logger.info(f"Signed in: {email}")
        return id_token
    except Exception as e:
        raise RuntimeError(f"Authentication failed: {str(e)}")

# ─── Synthesia workspace ───────────────────────────────────────────────────────

def create_workspace(id_token):
    headers = {
        "Authorization": id_token,
        "Content-Type": "application/json",
    }
    
    # Get or create workspace
    res = requests.get("https://api.synthesia.io/workspaces?scope=public", headers=headers)
    res.raise_for_status()
    data = res.json()
    
    if data.get("results") and len(data["results"]) > 0:
        workspace_id = data["results"][0]["id"]
        logger.info(f"Using existing workspace: {workspace_id}")
    else:
        res = requests.post(
            "https://api.synthesia.io/workspaces",
            headers=headers,
            json={"strict": True, "includeDemoVideos": False},
        )
        res.raise_for_status()
        workspace_id = res.json()["workspace"]["id"]
        logger.info(f"Created new workspace: {workspace_id}")

    # Complete onboarding steps
    try:
        requests.post(
            "https://api.synthesia.io/user/onboarding/setPreferredWorkspaceId",
            headers=headers,
            json={"workspaceId": workspace_id},
        )
        requests.post(
            "https://api.synthesia.io/billing/self-serve/" + workspace_id + "/paywall",
            headers=headers,
            json={
                "targetPlan": "freemium",
                "redirectUrl": "https://app.synthesia.io/#/?plan_created=true&payment_plan=freemium",
            },
        )
    except Exception as e:
        logger.warning(f"Onboarding step failed: {e}")
    
    time.sleep(5)
    return workspace_id

# ─── Synthesia video generation ───────────────────────────────────────────────

def start_synthesia_generation(token, workspace_id, prompt, aspect_ratio):
    model_request = {
        "modelName": "sora_2",
        "generateAudio": True,
        "aspectRatio": aspect_ratio,
    }
    
    r = requests.post(
        "https://api.prd.synthesia.io/avatarServices/api/generatedMedia/stockFootage/bulk?numberOfResults=1",
        headers={"Authorization": token, "Content-Type": "application/json"},
        json={
            "mediaType": "video",
            "modelRequest": model_request,
            "userPrompt": prompt,
            "workspaceId": workspace_id,
        },
        timeout=30,
    )
    r.raise_for_status()
    result = r.json()
    
    if not result or len(result) == 0:
        raise RuntimeError("No asset ID returned")
    
    asset_id = result[0]["mediaAssetId"]
    logger.info(f"Started generation, asset ID: {asset_id}")
    return asset_id

def poll_synthesia(token, asset_id, timeout=600, interval=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            f"https://api.synthesia.io/assets/{asset_id}",
            headers={"Authorization": token},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("uploadMetadata", {}).get("status", "unknown")
        
        logger.info(f"Polling status: {status}")
        
        if status == "ready":
            return data
        if status == "failed":
            raise RuntimeError("Generation failed on Synthesia side")
        
        time.sleep(interval)
    
    raise TimeoutError("Generation timed out after 10 minutes")

def generate_sora_video(prompt: str, aspect_ratio: str = "9:16") -> dict:
    """Generate a Sora video and return the video URL"""
    
    logger.info(f"Starting video generation with prompt: {prompt}, ratio: {aspect_ratio}")
    
    # Create temporary email
    temp = TempEmail()
    email = temp.generate()
    logger.info(f"Email created: {email}")
    
    # Sign up
    sign_up_result = sign_up_with_cognito(email)
    
    # Wait for verification code
    logger.info("Waiting for verification code...")
    code = temp.wait_for_code(timeout=120)
    if not code:
        raise RuntimeError("Timed out waiting for email verification code")
    logger.info(f"Verification code received: {code}")
    
    # Confirm sign up
    confirm_sign_up_with_cognito(email, code)
    
    # Sign in
    token = sign_in_with_cognito(email)
    
    # Create workspace
    workspace_id = create_workspace(token)
    
    # Start generation
    asset_id = start_synthesia_generation(token, workspace_id, prompt, aspect_ratio)
    
    # Poll for completion
    result = poll_synthesia(token, asset_id)
    
    video_url = result.get("url", "")
    if not video_url:
        raise RuntimeError("No video URL in response")
    
    logger.info(f"Video generated successfully: {video_url}")
    
    return {
        "video_url": video_url,
        "email": email,
        "asset_id": asset_id
    }

# ─── Background task ───────────────────────────────────────────────────────────

def run_generation_task(job_id, prompt, aspect_ratio):
    """Background task to generate video and update job status"""
    try:
        # Update status to processing
        jobs[job_id]["status"] = "processing"
        jobs[job_id]["message"] = "Starting video generation..."
        logger.info(f"Job {job_id}: Starting generation")
        
        # Generate the video
        result = generate_sora_video(prompt, aspect_ratio)
        
        # Update job with success
        jobs[job_id]["status"] = "done"
        jobs[job_id]["error"] = None
        jobs[job_id]["video"] = result["video_url"]
        jobs[job_id]["message"] = "Video generated successfully"
        jobs[job_id]["completed_at"] = time.time()
        
        logger.info(f"Job {job_id}: Completed successfully")
        
    except Exception as e:
        # Update job with error
        error_msg = str(e)
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = error_msg
        jobs[job_id]["video"] = None
        jobs[job_id]["message"] = f"Generation failed: {error_msg}"
        jobs[job_id]["completed_at"] = time.time()
        
        logger.error(f"Job {job_id}: Failed with error: {error_msg}")
    
    finally:
        # Schedule cleanup after 30 minutes
        def cleanup():
            time.sleep(30 * 60)
            if job_id in jobs:
                del jobs[job_id]
                logger.info(f"Job {job_id}: Cleaned up from memory")
        
        cleanup_thread = threading.Thread(target=cleanup)
        cleanup_thread.daemon = True
        cleanup_thread.start()

# ─── Flask Routes ───────────────────────────────────────────────────────────

@app.route('/ping', methods=['GET'])
def ping():
    """Health check endpoint"""
    return "pong"

@app.route('/generate', methods=['GET'])
def generate_video():
    """Start video generation"""
    prompt = request.args.get('prompt')
    aspect_ratio = request.args.get('size', '9:16')
    
    if not prompt:
        return jsonify({"error": "Missing 'prompt' parameter"}), 400
    
    if aspect_ratio not in ['9:16', '16:9']:
        return jsonify({"error": "Invalid size. Use '9:16' or '16:9'"}), 400
    
    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "error": None,
        "video": None,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "created_at": time.time(),
        "message": "Job created, starting soon"
    }
    
    # Start background task
    thread = threading.Thread(target=run_generation_task, args=(job_id, prompt, aspect_ratio))
    thread.daemon = True
    thread.start()
    
    logger.info(f"Created job {job_id} for prompt: {prompt}")
    
    return jsonify({
        "job_id": job_id,
        "message": "Video generation started",
        "status_url": f"/status?jobid={job_id}"
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get job status"""
    job_id = request.args.get('jobid')
    
    if not job_id:
        return jsonify({"error": "Missing 'jobid' parameter"}), 400
    
    job = jobs.get(job_id)
    
    if not job:
        return jsonify({"error": "Job not found"}), 404
    
    response = {
        "status": job["status"],
        "error": job.get("error"),
        "video": job.get("video")
    }
    
    if "message" in job:
        response["message"] = job["message"]
    
    return jsonify(response)

@app.route('/jobs', methods=['GET'])
def list_jobs():
    """List all active jobs"""
    active_jobs = {}
    for job_id, job in jobs.items():
        active_jobs[job_id] = {
            "status": job["status"],
            "prompt": job["prompt"],
            "created_at": job["created_at"],
            "message": job.get("message")
        }
    
    return jsonify({
        "active_jobs": len(active_jobs),
        "jobs": active_jobs
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
