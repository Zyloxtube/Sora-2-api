import os
import re
import time
import threading
import uuid
import logging
from flask import Flask, request, jsonify
from pycognito import Cognito
import requests
import traceback

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
        try:
            r = requests.get(f"{GUERRILLA_API}?f=get_email_address", timeout=30)
            r.raise_for_status()
            data = r.json()
            self.sid_token = data["sid_token"]
            self.seq = 0
            self.seen_ids = set()
            raw = data["email_addr"]
            at = raw.find("@")
            self.email_addr = (raw[:at + 1] if at != -1 else raw + "@") + "sharklasers.com"
            logger.info(f"Generated email: {self.email_addr}")
            return self.email_addr
        except Exception as e:
            logger.error(f"Failed to generate email: {e}")
            raise

    def check_inbox(self):
        if not self.sid_token:
            return None
        try:
            r = requests.get(
                f"{GUERRILLA_API}?f=check_email&sid_token={self.sid_token}&seq={self.seq}",
                timeout=30,
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
                timeout=30,
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

    def wait_for_code(self, timeout=120, interval=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = self.check_inbox()
            if code:
                return code
            logger.info(f"Waiting for code... {int(deadline - time.time())}s remaining")
            time.sleep(interval)
        return None

# ─── Cognito auth (FIXED - exactly like Discord bot) ─────────────────────────

def sign_up_with_cognito(email):
    try:
        logger.info(f"Attempting to sign up with email: {email}")
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        # Set attributes directly without add_custom_attributes
        cognito.email = email
        cognito.given_name = "User"
        cognito.family_name = "Test"
        # Register the user
        cognito.register(username=email, password=PASSWORD)
        logger.info(f"Sign up successful for: {email}")
        return {"status": "success", "message": "User signed up, waiting for confirmation"}
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sign up error: {error_msg}")
        if "User already exists" in error_msg or "UsernameExistsException" in error_msg:
            logger.info(f"User already exists: {email}")
            return {"status": "exists", "message": "User already exists"}
        raise RuntimeError(f"Sign-up failed: {error_msg}")

def confirm_sign_up_with_cognito(email, code):
    try:
        logger.info(f"Confirming sign up for: {email} with code: {code}")
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        cognito.confirm_sign_up(confirmation_code=code)
        logger.info(f"Confirmation successful for: {email}")
        return True
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Confirmation error: {error_msg}")
        raise RuntimeError(f"Confirmation failed: {error_msg}")

def sign_in_with_cognito(email):
    try:
        logger.info(f"Signing in with email: {email}")
        cognito = Cognito(
            user_pool_id=USER_POOL_ID,
            client_id=COGNITO_CLIENT_ID,
            username=email,
            user_pool_region="eu-west-1",
        )
        cognito.authenticate(password=PASSWORD)
        id_token = cognito.id_token
        if not id_token:
            raise RuntimeError("Failed to get ID token after authentication")
        logger.info(f"Sign in successful for: {email}")
        return id_token
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Sign in error: {error_msg}")
        if "NEW_PASSWORD_REQUIRED" in error_msg:
            try:
                cognito = Cognito(
                    user_pool_id=USER_POOL_ID,
                    client_id=COGNITO_CLIENT_ID,
                    username=email,
                    user_pool_region="eu-west-1",
                )
                cognito.authenticate(password=PASSWORD)
                if hasattr(cognito, "new_password_required") and cognito.new_password_required:
                    cognito.set_new_password_challenge(PASSWORD)
                    cognito.authenticate(password=PASSWORD)
                return cognito.id_token
            except Exception as inner_e:
                raise RuntimeError(f"Failed to handle password change: {str(inner_e)}")
        raise RuntimeError(f"Authentication failed: {error_msg}")

# ─── Synthesia workspace ───────────────────────────────────────────────────────

def create_workspace(id_token):
    headers = {
        "Authorization": id_token,
        "Content-Type": "application/json",
    }
    
    logger.info("Getting or creating workspace...")
    res = requests.get("https://api.synthesia.io/workspaces?scope=public", headers=headers, timeout=30)
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
            timeout=30,
        )
        res.raise_for_status()
        workspace_id = res.json()["workspace"]["id"]
        logger.info(f"Created new workspace: {workspace_id}")

    # Complete onboarding steps (non-critical, ignore errors)
    try:
        requests.post(
            "https://api.synthesia.io/user/onboarding/setPreferredWorkspaceId",
            headers=headers,
            json={"workspaceId": workspace_id},
            timeout=10,
        )
    except Exception:
        pass
    
    try:
        requests.post(
            "https://api.synthesia.io/user/onboarding/initialize",
            headers=headers,
            json={
                "featureFlags": {"freemiumEnabled": True},
                "queryParams": {"paymentPlanType": "free"},
                "allowReinitialize": False,
            },
            timeout=10,
        )
    except Exception:
        pass
    
    for _ in range(5):
        try:
            res = requests.post(
                "https://api.synthesia.io/user/onboarding/completeCurrentStep",
                headers=headers,
                json={"featureFlags": {"freemiumEnabled": True}},
                timeout=10,
            )
            if res.status_code != 200:
                break
        except Exception:
            break
    
    try:
        requests.post(
            "https://api.synthesia.io/user/questionnaire",
            headers=headers,
            json={
                "company": {"size": "emerging", "industry": "professional_services"},
                "seniority": "individual_contributor",
                "persona": "marketing",
            },
            timeout=10,
        )
    except Exception:
        pass
    
    try:
        requests.post(
            "https://api.synthesia.io/user/signupForm",
            headers=headers,
            json={"analyticsCookies": {}},
            timeout=10,
        )
    except Exception:
        pass
    
    try:
        requests.post(
            f"https://api.synthesia.io/billing/self-serve/{workspace_id}/paywall",
            headers=headers,
            json={
                "targetPlan": "freemium",
                "redirectUrl": "https://app.synthesia.io/#/?plan_created=true&payment_plan=freemium",
            },
            timeout=10,
        )
    except Exception:
        pass
    
    logger.info("Workspace setup complete")
    time.sleep(30)  # Wait for workspace to be fully ready
    return workspace_id

# ─── Synthesia video generation ───────────────────────────────────────────────

def start_synthesia_generation(token, workspace_id, prompt, aspect_ratio):
    try:
        logger.info(f"Starting video generation with prompt: {prompt}")
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
            timeout=60,
        )
        r.raise_for_status()
        result = r.json()
        if not result or len(result) == 0:
            raise RuntimeError("No asset ID returned")
        asset_id = result[0]["mediaAssetId"]
        logger.info(f"Generation started, asset ID: {asset_id}")
        return asset_id
    except Exception as e:
        logger.error(f"Failed to start generation: {e}")
        raise

def poll_synthesia(token, asset_id, timeout=600, interval=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(
                f"https://api.synthesia.io/assets/{asset_id}",
                headers={"Authorization": token},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            status = data.get("uploadMetadata", {}).get("status", "unknown")
            logger.info(f"Polling status for {asset_id}: {status}")
            
            if status == "ready":
                return data
            if status == "failed":
                raise RuntimeError("Generation failed on Synthesia side")
            
            time.sleep(interval)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(interval)
    
    raise TimeoutError("Generation timed out after 10 minutes")

def generate_sora_video(prompt: str, aspect_ratio: str = "9:16", job_id: str = None, update_callback=None) -> dict:
    """Generate a Sora video with progress updates"""
    
    def update_status(status, message):
        if update_callback and job_id:
            update_callback(job_id, status, message)
    
    try:
        update_status("accgen", "Creating temporary email...")
        temp = TempEmail()
        email = temp.generate()
        logger.info(f"Email created: {email}")
        
        update_status("accgen", f"Signing up with email: {email}...")
        sign_up_result = sign_up_with_cognito(email)
        
        update_status("accgen", "Waiting for verification code (checking inbox)...")
        code = temp.wait_for_code(timeout=120)
        if not code:
            raise RuntimeError("Timed out waiting for email verification code")
        logger.info(f"Verification code received: {code}")
        
        update_status("accgen", "Confirming email verification...")
        confirm_sign_up_with_cognito(email, code)
        
        update_status("accgen", "Signing in to account...")
        token = sign_in_with_cognito(email)
        
        update_status("accgen", "Setting up Synthesia workspace...")
        workspace_id = create_workspace(token)
        
        update_status("processing", "Starting video generation...")
        asset_id = start_synthesia_generation(token, workspace_id, prompt, aspect_ratio)
        
        update_status("processing", "Generating video (this may take several minutes)...")
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
        
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        logger.error(traceback.format_exc())
        raise

# ─── Background task with progress updates ───────────────────────────────────

def update_job_status(job_id, status, message):
    """Callback to update job status"""
    if job_id in jobs:
        jobs[job_id]["status"] = status
        jobs[job_id]["message"] = message
        logger.info(f"Job {job_id}: {status} - {message}")

def run_generation_task(job_id, prompt, aspect_ratio):
    """Background task to generate video with detailed progress"""
    try:
        # Generate the video with progress updates
        result = generate_sora_video(prompt, aspect_ratio, job_id, update_job_status)
        
        # Update job with success
        jobs[job_id]["status"] = "done"
        jobs[job_id]["error"] = None
        jobs[job_id]["video"] = result["video_url"]
        jobs[job_id]["message"] = "Video generated successfully!"
        jobs[job_id]["completed_at"] = time.time()
        
        logger.info(f"Job {job_id}: Completed successfully")
        
    except Exception as e:
        error_msg = str(e)
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = error_msg
        jobs[job_id]["video"] = None
        jobs[job_id]["message"] = f"Failed: {error_msg}"
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
    return "pong"

@app.route('/generate', methods=['GET'])
def generate_video():
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
