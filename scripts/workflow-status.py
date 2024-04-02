import requests
import time
import sys
import subprocess

# Check for the right number of arguments
if len(sys.argv) != 4:
    print("Usage: python script.py <GITHUB_TOKEN> <GITHUB_REPOSITORY> <GITHUB_RUN_ID>")
    sys.exit(1)

GITHUB_TOKEN = sys.argv[1]
GITHUB_REPOSITORY = sys.argv[2]
GITHUB_RUN_ID = sys.argv[3]

max_attempts = 10
page = 1
conclusion_counts = {status: 0 for status in ["success", "failure", "cancelled", "skipped", "unknown"]}

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}


def fetch_jobs(page):
    try:
        response = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/runs/{GITHUB_RUN_ID}/jobs",
            params={"page": page, "per_page": 100},
            headers=headers
        )
        response.raise_for_status()  # This will raise an exception for HTTP error codes
        # Returning both the JSON data and the Link header for pagination
        return response.json(), response.headers.get("Link", "")
    except requests.exceptions.HTTPError as e:
        print(f"HTTPError: {e.response.status_code} - {e.response.text}")
    except requests.exceptions.RequestException as e:
        print(f"RequestException: {e}")
    return None, None


while True:
    success = False
    for attempt in range(1, max_attempts + 1):
        print(f"Attempt {attempt} of {max_attempts} for page {page}")

        jobs_data, link_header = fetch_jobs(page)

        if jobs_data and 'jobs' in jobs_data and jobs_data['jobs']:
            print("API Request successful.")
            for job in jobs_data['jobs']:
                conclusion = job.get('conclusion', 'unknown') or 'unknown'
                conclusion_counts[conclusion] += 1
            success = True
            break
        else:
            print(f"API Request failed or no jobs found, retrying in {attempt * 2} seconds...")
            time.sleep(attempt * 2)

    if not success:
        print("API requests failed after {} attempts, setting WORKFLOW_CONCLUSION to failure.")
        WORKFLOW_CONCLUSION = "failure"
        break

    # Check for the next page using the Link header
    if 'rel="next"' in link_header:
        page += 1
    else:
        break

# Determine overall workflow conclusion if not already set to failure
if 'WORKFLOW_CONCLUSION' not in globals() or WORKFLOW_CONCLUSION != "failure":
    if conclusion_counts['cancelled'] > 0:
        WORKFLOW_CONCLUSION = "cancelled"
    elif conclusion_counts['failure'] > 0:
        WORKFLOW_CONCLUSION = "failure"
    elif conclusion_counts['success'] > 0:
        WORKFLOW_CONCLUSION = "success"
    else:
        WORKFLOW_CONCLUSION = "failure"

subprocess.run(f"echo \"WORKFLOW_CONCLUSION={WORKFLOW_CONCLUSION}\" >> $GITHUB_ENV", shell=True, check=True)
