import json
import sys
import subprocess

def analyze_job_context(job_context_str):
    try:
        job_context = json.loads(job_context_str)
    except json.JSONDecodeError:
        print("Error: Failed to decode the job context.")
        sys.exit(1)

    conclusion_counts = {"success": 0, "failure": 0, "cancelled": 0, "skipped": 0}

    for job in job_context:
        result = job_context[job].get("result", "unknown")
        if result in conclusion_counts:
            conclusion_counts[result] += 1
        else:
            # Treat any result not explicitly counted as "failure"
            conclusion_counts["failure"] += 1

    if conclusion_counts["cancelled"] > 0:
        return "cancelled"
    elif conclusion_counts["failure"] > 0:
        return "failure"
    elif conclusion_counts["success"] > 0:
        return "success"
    else:
        # If for some reason the above conditions didn't catch,
        # default to "failure" to ensure a conclusion is always set.
        return "failure"

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py '<job_context_json>'")
        sys.exit(1)
    
    job_context_str = sys.argv[1]
    workflow_conclusion = analyze_job_context(job_context_str)

    subprocess.run(f"echo \"WORKFLOW_CONCLUSION={workflow_conclusion}\" >> $GITHUB_ENV", shell=True, check=True)
