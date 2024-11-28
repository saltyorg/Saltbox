#!/bin/bash

set -euo pipefail

# Check required environment variables
required_vars=("GITHUB_REPOSITORY" "GITHUB_TOKEN" "DISCORD_WEBHOOK" "WORKFLOW_RUN_ID")
for var in "${required_vars[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo "Error: $var is not set"
        exit 1
    fi
done

# Get workflow run information
workflow_run_info=$(gh api "/repos/${GITHUB_REPOSITORY}/actions/runs/${WORKFLOW_RUN_ID}")

# Extract basic workflow information
workflow_name=$(echo "$workflow_run_info" | jq -r '.name')
workflow_conclusion=$(echo "$workflow_run_info" | jq -r '.conclusion')
attempt=$(echo "$workflow_run_info" | jq -r '.run_attempt')

# Check if we should send notification
if [ "$workflow_conclusion" = "failure" ] && [ "$attempt" -le 2 ]; then
    echo "Skipping notification for failed attempt $attempt (waiting for retry)"
    exit 0
fi

# Continue with rest of script only if we should send notification
workflow_url=$(echo "$workflow_run_info" | jq -r '.html_url')
triggered_by=$(echo "$workflow_run_info" | jq -r '.triggering_actor.login')
ref=$(echo "$workflow_run_info" | jq -r '.head_branch')
head_sha=$(echo "$workflow_run_info" | jq -r '.head_sha')
event_name=$(echo "$workflow_run_info" | jq -r '.event')

# Function to get Discord color based on conclusion
get_discord_color() {
    case $1 in
        success) echo 2664261 ;; # 0x28A745
        failure) echo 13318193 ;; # 0xCB2431
        cancelled) echo 14390537 ;; # 0xDBAB09
        *) echo 15844367 ;; # 0xF1C232 (default)
    esac
}

# Function to get event-specific field
get_event_field() {
    local event_name=$1
    local event_field=""
    
    case $event_name in
        pull_request|pull_request_target)
            pr_number=$(echo "$workflow_run_info" | jq -r '.pull_requests[0].number')
            if [ "$pr_number" != "null" ]; then
                pr_info=$(gh api "/repos/${GITHUB_REPOSITORY}/pulls/${pr_number}")
                pr_title=$(echo "$pr_info" | jq -r '.title')
                pr_url=$(echo "$pr_info" | jq -r '.html_url')
                event_field="{\"name\":\"Event - ${event_name}\",\"value\":\"[#${pr_number}](${pr_url}) ${pr_title}\"}"
            else
                event_field="{\"name\":\"Event\",\"value\":\"${event_name}\"}"
            fi
            ;;
        push)
            commit_info=$(gh api "/repos/${GITHUB_REPOSITORY}/commits/${head_sha}")
            commit_msg=$(echo "$commit_info" | jq -r '.commit.message[0:4000]' | jq -R -s '.')
            short_sha="${head_sha:0:7}"
            commit_url=$(echo "$commit_info" | jq -r '.html_url')
            event_field="{\"name\":\"Event - ${event_name}\",\"value\":\"[\`${short_sha}\`](${commit_url}) ${commit_msg}\"}"
            ;;
        release)
            release_info=$(gh api "/repos/${GITHUB_REPOSITORY}/releases/latest")
            release_tag=$(echo "$release_info" | jq -r '.tag_name')
            release_name=$(echo "$release_info" | jq -r '.name')
            release_url=$(echo "$release_info" | jq -r '.html_url')
            event_field="{\"name\":\"Event - ${event_name}\",\"value\":\"[${release_name}](${release_url}) - ${release_tag}\"}"
            ;;
        workflow_dispatch)
            event_field="{\"name\":\"Event - ${event_name}\",\"value\":\"Workflow manually triggered by ${triggered_by}\"}"
            ;;
        *)
            event_field="{\"name\":\"Event\",\"value\":\"${event_name}\"}"
            ;;
    esac
    
    echo "$event_field"
}

# Prepare Discord webhook payload
color=$(get_discord_color "$workflow_conclusion")
title="${workflow_conclusion^}: ${workflow_name}"
description="Run attempt: ${attempt}"
event_field=$(get_event_field "$event_name")

payload=$(cat <<EOF
{
  "embeds": [
    {
      "title": "$title",
      "description": "$description",
      "color": $color,
      "fields": [
        {
          "name": "Repository",
          "value": "[$GITHUB_REPOSITORY](https://github.com/$GITHUB_REPOSITORY)",
          "inline": true
        },
        {
          "name": "Ref",
          "value": "$ref",
          "inline": true
        },
        $event_field,
        {
          "name": "Triggered by",
          "value": "$triggered_by",
          "inline": true
        },
        {
          "name": "Workflow",
          "value": "[$workflow_name]($workflow_url)",
          "inline": true
        }
      ],
      "timestamp": "$(date -u +'%Y-%m-%dT%H:%M:%S.000Z')"
    }
  ]
}
EOF
)

# Send Discord notification
curl -H "Content-Type: application/json" -d "$payload" "$DISCORD_WEBHOOK"
