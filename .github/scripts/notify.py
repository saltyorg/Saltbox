#!/usr/bin/env python3

import os
import sys
import json
from datetime import datetime, timezone
import requests
import time
from typing import Dict, Any, Optional

DISCORD_FIELD_VALUE_LIMIT = 1024  # Discord's limit for field values
MAX_RETRIES = 10  # Maximum number of retries for fetching workflow data
RETRY_DELAY = 30  # Delay between retries in seconds

# Define valid conclusion states for a finished GitHub Actions run
VALID_CONCLUSIONS: Set[str] = {
    "success",
    "failure",
    "cancelled"
}

def check_required_vars() -> None:
    """Check if all required environment variables are set."""
    required = ["GITHUB_REPOSITORY", "GITHUB_TOKEN", "DISCORD_WEBHOOK", "WORKFLOW_RUN_ID"]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

def get_github_data(url: str) -> Dict[str, Any]:
    """Make a GitHub API request with error handling."""
    print(f"Fetching GitHub data from: {url}")
    headers = {"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    print(f"Received data: {json.dumps(data, indent=2)}")
    return data

def get_workflow_data_with_retry(repo: str, run_id: str) -> Dict[str, Any]:
    """
    Fetch workflow data with retries until we get a valid conclusion state.
    Raises an error if no valid conclusion after max retries.
    """
    for attempt in range(MAX_RETRIES):
        workflow_data = get_github_data(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
        )
        
        conclusion = workflow_data.get("conclusion")
        if conclusion in VALID_CONCLUSIONS:
            print(f"Got valid conclusion after {attempt + 1} attempts: {conclusion}")
            return workflow_data
            
        print(f"Attempt {attempt + 1}/{MAX_RETRIES}: Conclusion '{conclusion}' not in expected states {VALID_CONCLUSIONS}, retrying in {RETRY_DELAY} seconds...")
        time.sleep(RETRY_DELAY)
    
    raise RuntimeError(f"Failed to get valid workflow conclusion after {MAX_RETRIES} retries. Last conclusion: {conclusion}")

def get_discord_color(conclusion: str) -> int:
    """Get Discord color code based on workflow conclusion."""
    return {
        "success": 0x28A745,
        "failure": 0xCB2431,
        "cancelled": 0xDBAB09
    }.get(conclusion, 0xF1C232)

def truncate_commit_message(sha_url: str, message: str) -> str:
    """
    Truncate commit message to fit Discord's field value limit while preserving markdown.
    Includes space for the SHA link at the start.
    """
    sha_part = f"[`{sha_url}`] "
    max_message_length = DISCORD_FIELD_VALUE_LIMIT - len(sha_part)
    
    if len(message) <= max_message_length:
        return message
    
    return message[:max_message_length-3] + "..."

def get_event_field(event_name: str, workflow_data: Dict[str, Any]) -> Dict[str, str]:
    """Generate event-specific field for Discord embed."""
    print(f"Processing event type: {event_name}")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    
    if event_name in ("pull_request", "pull_request_target"):
        pr_number = workflow_data.get("pull_requests", [{}])[0].get("number")
        if pr_number:
            pr_data = get_github_data(f"https://api.github.com/repos/{repo}/pulls/{pr_number}")
            return {
                "name": f"Event - {event_name}",
                "value": f"[#{pr_number}]({pr_data['html_url']}) {pr_data['title']}"
            }
            
    elif event_name == "push":
        head_sha = workflow_data["head_sha"]
        commit_data = get_github_data(f"https://api.github.com/repos/{repo}/commits/{head_sha}")
        commit_msg = commit_data["commit"]["message"].replace("\r\n", "\n").strip()
        
        # Create the SHA URL part
        sha_url = f"{head_sha[:7]}]({commit_data['html_url']}"
        
        # Truncate message if needed
        truncated_msg = truncate_commit_message(sha_url, commit_msg)
        
        return {
            "name": f"Event - {event_name}",
            "value": f"[{sha_url}) {truncated_msg}"
        }
        
    elif event_name == "release":
        release_data = get_github_data(f"https://api.github.com/repos/{repo}/releases/latest")
        return {
            "name": f"Event - {event_name}",
            "value": f"[{release_data['name']}]({release_data['html_url']}) - {release_data['tag_name']}"
        }
        
    elif event_name == "workflow_dispatch":
        return {
            "name": f"Event - {event_name}",
            "value": f"Workflow manually triggered by {workflow_data['triggering_actor']['login']}"
        }
        
    return {
        "name": "Event",
        "value": event_name
    }

def main() -> None:
    """Main function to process workflow and send Discord notification."""
    print("Starting Discord notification script")
    check_required_vars()
    
    # Get environment variables
    repo = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("WORKFLOW_RUN_ID", "")
    webhook_url = os.getenv("DISCORD_WEBHOOK", "")
    
    # Fetch workflow data with retries
    try:
        workflow_data = get_workflow_data_with_retry(repo, run_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    # Extract basic information
    workflow_name = workflow_data["name"]
    conclusion = workflow_data["conclusion"]
    attempt = workflow_data["run_attempt"]
    
    # Skip notification for early failure attempts
    if conclusion == "failure" and attempt <= 2:
        print(f"Skipping notification for failed attempt {attempt} (waiting for retry)")
        return
    
    # Prepare Discord embed
    embed = {
        "title": f"{conclusion.capitalize()}: {workflow_name}",
        "description": f"Run attempt: {attempt}",
        "color": get_discord_color(conclusion),
        "fields": [
            {
                "name": "Repository",
                "value": f"[{repo}](https://github.com/{repo})",
                "inline": True
            },
            {
                "name": "Ref",
                "value": workflow_data["head_branch"],
                "inline": True
            },
            get_event_field(workflow_data["event"], workflow_data),
            {
                "name": "Triggered by",
                "value": workflow_data["triggering_actor"]["login"],
                "inline": True
            },
            {
                "name": "Workflow",
                "value": f"[{workflow_name}]({workflow_data['html_url']})",
                "inline": True
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    # Prepare and send Discord webhook
    payload = {"embeds": [embed]}
    print(f"Sending Discord payload: {json.dumps(payload, indent=2)}")
    
    response = requests.post(webhook_url, json=payload)
    print(f"Discord API response status code: {response.status_code}")
    
    if response.status_code != 204:
        print(f"Error response from Discord: {response.text}")
        sys.exit(1)
    
    print("Discord notification sent successfully")

if __name__ == "__main__":
    main()
