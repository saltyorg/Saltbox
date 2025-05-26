#!/usr/bin/env python3

import os
import sys
import json
from datetime import datetime, timezone
import requests
import time
from typing import Dict, Any, Set, Optional

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
    if missing := [var for var in required if not os.getenv(var)]:
        print(f"Error: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def get_github_data(url: str) -> Optional[Dict[str, Any]]:
    """Make a GitHub API request with error handling."""
    print(f"Fetching GitHub data from: {url}")
    headers = {"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"}
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        print(f"Received data: {json.dumps(data, indent=2)}")
        return data
    except requests.exceptions.RequestException as e:
        print(f"Error fetching GitHub data: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON response: {e}")
        return None


def get_workflow_data_with_retry(repo: str, run_id: str) -> Dict[str, Any]:
    """
    Fetch workflow data with retries until we get a valid conclusion state.
    Raises an error if no valid conclusion after max retries.
    """
    last_conclusion = None
    for attempt in range(MAX_RETRIES):
        workflow_data = get_github_data(
            f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
        )
        
        if workflow_data is None:
            print(f"Attempt {attempt + 1}/{MAX_RETRIES}: Failed to fetch workflow data, retrying in {RETRY_DELAY} seconds...")
            time.sleep(RETRY_DELAY)
            continue

        conclusion = workflow_data.get("conclusion")
        status = workflow_data.get("status")
        
        # Log unexpected data structure
        if "conclusion" not in workflow_data:
            print(f"WARNING: No 'conclusion' field in workflow data")
            print(f"Available workflow data keys: {list(workflow_data.keys())}")
        if "status" not in workflow_data:
            print(f"WARNING: No 'status' field in workflow data")
            
        if conclusion in VALID_CONCLUSIONS:
            print(f"Got valid conclusion after {attempt + 1} attempts: {conclusion}")
            return workflow_data

        print(
            f"Attempt {attempt + 1}/{MAX_RETRIES}: Conclusion '{conclusion}' not in expected states {VALID_CONCLUSIONS}, retrying in {RETRY_DELAY} seconds...")
        print(f"Current workflow status: {status}")
        last_conclusion = conclusion
        time.sleep(RETRY_DELAY)

    raise RuntimeError(
        f"Failed to get valid workflow conclusion after {MAX_RETRIES} retries. Last conclusion: {last_conclusion}")


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

    return message[:max_message_length - 3] + "..."


def safe_get_nested(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely get nested dictionary values."""
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
            if data is None:
                return default
        else:
            return default
    return data


def get_event_field(event_name: str, workflow_data: Dict[str, Any]) -> Dict[str, str]:
    """Generate event-specific field for Discord embed."""
    print(f"Processing event type: {event_name}")
    repo = os.getenv("GITHUB_REPOSITORY", "")

    if event_name in ("pull_request", "pull_request_target"):
        # For PR events, we need to find the PR number from the head branch and SHA
        head_sha = workflow_data.get("head_sha")
        head_branch = workflow_data.get("head_branch")
        
        if not head_sha:
            print(f"WARNING: No head_sha found for {event_name} event")
            return {
                "name": f"Event - {event_name}",
                "value": f"Pull request event (branch: {head_branch or 'unknown'})"
            }
        
        # Get PR info directly from commits API (most reliable method)
        print(f"Getting PR info for commit SHA: {head_sha}")
        commit_pulls_data = get_github_data(f"https://api.github.com/repos/{repo}/commits/{head_sha}/pulls")
        
        if commit_pulls_data and len(commit_pulls_data) > 0:
            pr_data = commit_pulls_data[0]
            pr_number = pr_data.get("number")
            pr_title = pr_data.get("title", "No title")
            pr_url = pr_data.get("html_url", f"https://github.com/{repo}/pull/{pr_number}")
            
            print(f"Found PR #{pr_number}: {pr_title}")
            return {
                "name": f"Event - {event_name}",
                "value": f"[#{pr_number}]({pr_url}) {pr_title}"
            }
        else:
            print(f"No PRs found via commits API, trying search fallback")
            
            # Fallback: use GitHub Search API
            search_url = f"https://api.github.com/search/issues?q=repo:{repo}+type:pr+sha:{head_sha}"
            search_data = get_github_data(search_url)
            
            if search_data and search_data.get("total_count", 0) > 0:
                items = search_data.get("items", [])
                if items:
                    pr_item = items[0]
                    pr_number = pr_item.get("number")
                    pr_title = pr_item.get("title", "No title")
                    pr_url = pr_item.get("html_url", f"https://github.com/{repo}/pull/{pr_number}")
                    
                    print(f"Found PR via search: #{pr_number}: {pr_title}")
                    return {
                        "name": f"Event - {event_name}",
                        "value": f"[#{pr_number}]({pr_url}) {pr_title}"
                    }
            
            print(f"Could not find PR information via any method")
        
        # Fallback if we can't find the PR
        return {
            "name": f"Event - {event_name}",
            "value": f"Pull request event (branch: {head_branch}, SHA: {head_sha[:7]})"
        }

    elif event_name == "push":
        head_sha = workflow_data.get("head_sha")
        if not head_sha:
            print(f"WARNING: No head_sha found for push event")
            print(f"Available workflow_data keys: {list(workflow_data.keys())}")
        else:
            commit_data = get_github_data(f"https://api.github.com/repos/{repo}/commits/{head_sha}")
            if commit_data:
                commit_msg = safe_get_nested(commit_data, "commit", "message", default="No commit message")
                if commit_msg == "No commit message":
                    print(f"WARNING: No commit message found in commit data")
                    print(f"Commit data keys: {list(commit_data.keys())}")
                    if "commit" in commit_data:
                        print(f"Commit object keys: {list(commit_data['commit'].keys())}")
                
                commit_msg = commit_msg.replace("\r\n", "\n").strip()
                
                # Create the SHA URL part
                sha_short = head_sha[:7]
                commit_url = commit_data.get("html_url", f"https://github.com/{repo}/commit/{head_sha}")
                if not commit_data.get("html_url"):
                    print(f"WARNING: No html_url in commit data, using fallback URL")
                
                sha_url = f"{sha_short}]({commit_url}"
                
                # Truncate message if needed
                truncated_msg = truncate_commit_message(sha_url, commit_msg)
                
                return {
                    "name": f"Event - {event_name}",
                    "value": f"[{sha_url}) {truncated_msg}"
                }
            else:
                print(f"WARNING: Failed to fetch commit data for SHA: {head_sha}")

    elif event_name == "release":
        release_data = get_github_data(f"https://api.github.com/repos/{repo}/releases/latest")
        if release_data:
            release_name = release_data.get("name", "Unnamed release")
            release_url = release_data.get("html_url", f"https://github.com/{repo}/releases")
            tag_name = release_data.get("tag_name", "No tag")
            
            if release_name == "Unnamed release":
                print(f"WARNING: No release name found")
                print(f"Release data keys: {list(release_data.keys())}")
            if not release_data.get("html_url"):
                print(f"WARNING: No html_url in release data, using fallback URL")
            if tag_name == "No tag":
                print(f"WARNING: No tag_name found in release data")
                
            return {
                "name": f"Event - {event_name}",
                "value": f"[{release_name}]({release_url}) - {tag_name}"
            }
        else:
            print(f"WARNING: Failed to fetch release data")

    elif event_name == "workflow_dispatch":
        triggering_actor = safe_get_nested(workflow_data, "triggering_actor", "login", default="Unknown user")
        if triggering_actor == "Unknown user":
            print(f"WARNING: No triggering_actor.login found")
            print(f"Workflow data keys: {list(workflow_data.keys())}")
            if "triggering_actor" in workflow_data:
                print(f"Triggering actor type: {type(workflow_data['triggering_actor'])}")
                if isinstance(workflow_data['triggering_actor'], dict):
                    print(f"Triggering actor keys: {list(workflow_data['triggering_actor'].keys())}")
                    
        return {
            "name": f"Event - {event_name}",
            "value": f"Workflow manually triggered by {triggering_actor}"
        }

    print(f"INFO: Unhandled event type: {event_name}, using default format")
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

    # Extract basic information with defaults
    workflow_name = workflow_data.get("name", "Unknown workflow")
    conclusion = workflow_data.get("conclusion", "unknown")
    attempt = workflow_data.get("run_attempt", 1)

    # Skip notification for early failure attempts
    if conclusion == "failure" and attempt <= 2:
        print(f"Skipping notification for failed attempt {attempt} (waiting for retry)")
        return

    # Safely get nested values
    head_branch = workflow_data.get("head_branch", "Unknown branch")
    event_name = workflow_data.get("event", "Unknown event")
    triggering_actor_login = safe_get_nested(workflow_data, "triggering_actor", "login", default="Unknown user")
    workflow_url = workflow_data.get("html_url", f"https://github.com/{repo}/actions/runs/{run_id}")

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
                "value": head_branch,
                "inline": True
            },
            get_event_field(event_name, workflow_data),
            {
                "name": "Triggered by",
                "value": triggering_actor_login,
                "inline": True
            },
            {
                "name": "Workflow",
                "value": f"[{workflow_name}]({workflow_url})",
                "inline": True
            }
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # Prepare and send Discord webhook
    payload = {"embeds": [embed]}
    print(f"Sending Discord payload: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        print(f"Discord API response status code: {response.status_code}")

        if response.status_code != 204:
            print(f"Error response from Discord: {response.text}")
            sys.exit(1)

        print("Discord notification sent successfully")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Discord notification: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
