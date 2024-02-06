#!/bin/bash

# Pass GitHub token, repository, and run ID as arguments
GITHUB_TOKEN=$1
GITHUB_REPOSITORY=$2
GITHUB_RUN_ID=$3

max_attempts=5
page=1
declare -A conclusion_counts

# Initialize conclusions to avoid integer expression expected error
for status in success failure cancelled skipped unknown; do
  conclusion_counts[$status]=0
done

while :; do
  success=false
  for attempt in $(seq 1 $max_attempts); do
    echo "Attempt $attempt of $max_attempts for page $page"
    
    response_with_headers=$(curl -sS -I -H "Authorization: token $GITHUB_TOKEN" \
      "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/jobs?page=$page&per_page=100")
    
    link_header=$(echo "$response_with_headers" | grep -i '^Link:' | tr -d '\r')

    response=$(curl -sS -H "Authorization: token $GITHUB_TOKEN" \
      "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/jobs?page=$page&per_page=100")

    if [ $? -eq 0 ]; then
      echo "API Request successful."

      job_conclusions=$(echo "$response" | jq -r '.jobs[].conclusion')
      for line in $job_conclusions; do
        # Handle null or unexpected values
        if [[ -z "$line" || "$line" == "null" ]]; then
          ((conclusion_counts[unknown]++))
        else
          ((conclusion_counts[$line]++))
        fi
      done
      
      success=true
      break
    else
      echo "API Request failed, retrying in $((attempt * 2)) seconds..."
      sleep $((attempt * 2))
    fi
  done

  if [ "$success" = false ]; then
    echo "API requests failed after $max_attempts attempts, defaulting to failure."
    exit 1
  fi

  # Check for next page using the Link header
  if echo "$link_header" | grep -q 'rel="next"'; then
    page=$((page + 1))
  else
    break
  fi
done

# Determine overall workflow conclusion
if [ ${conclusion_counts[cancelled]} -gt 0 ]; then
  WORKFLOW_CONCLUSION="cancelled"
elif [ ${conclusion_counts[failure]} -gt 0 ]; then
  WORKFLOW_CONCLUSION="failure"
elif [ ${conclusion_counts[success]} -gt 0 ]; then
  WORKFLOW_CONCLUSION="success"
else
  WORKFLOW_CONCLUSION="failure"
fi

# Export WORKFLOW_CONCLUSION to GitHub Actions environment
echo "WORKFLOW_CONCLUSION=$WORKFLOW_CONCLUSION" >> $GITHUB_ENV
