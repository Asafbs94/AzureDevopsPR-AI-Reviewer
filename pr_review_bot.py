from llama_stack_client import LlamaStackClient
from llama_stack_client.types import UserMessage
from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta
from azure.devops.v7_0.git.models import GitPullRequestSearchCriteria,Comment, CommentThread,GitTargetVersionDescriptor,GitBaseVersionDescriptor
from datetime import timedelta
from flask import Flask
import difflib

# Load environment variables from .env file
load_dotenv()

# Azure DevOps Organization and Project details from environment variables
organization_url = os.getenv("AZURE_ORG_URL")
personal_access_token = os.getenv("AZURE_PAT")
project_name = os.getenv("PROJECT_NAME")
repository_id = os.getenv("REPO_ID")
max_tokens = os.getenv("MAX_TOKENS")
model_version = os.getenv("MODEL_VERSION")
flask_port = os.getenv("FLASK_PORT")
meta_llama_url = os.getenv("META_LLAMA_URL")
# List of authors to ignore
IGNORED_AUTHORS = os.getenv("IGNORED_AUTHORS", "NONE").split(",")

# Set up OpenAI API key from environment variables
OpenAI_api_key = os.getenv("OPENAI_API_KEY")
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    get_pull_requests()
    

# Authenticate to Azure DevOps
def get_azure_devops_connection():
    try:
        credentials = BasicAuthentication('', personal_access_token)
        connection = Connection(base_url=organization_url, creds=credentials)
        return connection
    except Exception as e:
        print(f"Error connecting to Azure DevOps: {str(e)}")
        return None

# Get pull requests from Azure DevOps
def get_pull_requests():
    try:
        connection = get_azure_devops_connection()
        if connection is None:
            return []

        criteria = GitPullRequestSearchCriteria(status='active')
        git_client = connection.clients.get_git_client()
        pull_requests = git_client.get_pull_requests(
            project=project_name,
            repository_id=repository_id,
            search_criteria=criteria
        )
        return pull_requests
    except Exception as e:
        print(f"Error fetching pull requests: {str(e)}")
        return []

# Check if PR author is ignored
def is_author_ignored(author):
    return author.lower() in [ignored_author.lower() for ignored_author in IGNORED_AUTHORS]

# Filter PRs created in the last 24 hours
def is_recent_pr(creation_date):
    now = datetime.utcnow()
    pr_date = datetime.strptime(creation_date, "%Y-%m-%dT%H:%M:%SZ")  # Azure DevOps uses ISO 8601 format
    return now - pr_date <= timedelta(days=1)

# Analyze the PR diff using OpenAI
def analyze_pr_diff(diff):
    try:
        prompt = (
            "Review the following pull request and provide a short 1 paragraph feedback for all modified files. "
            "Be short and summarized for every file; no more than 1 paragraph is allowed. "
            "Give attention to time complexity and clean code principles. Check for possible errors:\n\n"
        )
        
        for item in diff:
            file_name = item.get("file", "Unknown File")
            changes = item.get("changes", "No changes provided")
            prompt += f"File: {file_name}\n{changes}\n\n"
        
        # Initialize the client inside the try block
        client = LlamaStackClient(base_url=meta_llama_url)
        
        response = client.inference.chat_completion(
            messages=[UserMessage(content=prompt, role="user")],
            model=model_version,
            stream=False,
        )
        
        return response.choices[0].message['content']
    
    except Exception as e:
        print(f"Error analyzing PR : {str(e)}")
        return ""


# Comment on the pull request
def comment_on_pr(pr_id, comment):
    try:
        connection = get_azure_devops_connection()
        if connection is None:
            return

        git_client = connection.clients.get_git_client()

        # Create a comment thread
        thread = CommentThread(
            comments=[Comment(content=comment)],
            status="active"
        )

        # Add the comment thread to the pull request
        git_client.create_thread(
            repository_id=repository_id,
            project=project_name,
            pull_request_id=pr_id,
            comment_thread=thread
        )
        print(f"Comment posted on PR #{pr_id}")
    except Exception as e:
        print(f"Error commenting on PR {pr_id}: {str(e)}")

# Fetch the diff content of a pull request
def fetch_pr_diff(pr_id):
    connection = get_azure_devops_connection()
    git_client = connection.clients.get_git_client()

    # Get PR details
    pr = git_client.get_pull_request_by_id(pr_id)
    
    print(f"\nPR Details:")
    print(f"PR ID: {pr.pull_request_id}")
    print(f"Source: {pr.source_ref_name}")
    print(f"Target: {pr.target_ref_name}")
    
    # Get the PR changes directly using the iterations API
    iterations = git_client.get_pull_request_iterations(
        project=project_name,
        repository_id=repository_id,
        pull_request_id=pr_id
    )
    
    # Get the latest iteration
    latest_iteration = iterations[-1].id
    
    # Get changes for this iteration
    changes = git_client.get_pull_request_iteration_changes(
        project=project_name,
        repository_id=repository_id,
        pull_request_id=pr_id,
        iteration_id=latest_iteration
    )
    
    print(f"\nNumber of changes found: {len(changes.change_entries)}")
    
    changed_content = []
    for change in changes.change_entries:
        file_path = change.additional_properties['item']['path']
        print(f"\nProcessing {file_path}")
        
        try:
            # Get the PR version using get_item_content
            pr_content = git_client.get_item_content(
                repository_id=repository_id,
                project=project_name,
                path=file_path,
                download=True,  # Important: This forces content download
                version_descriptor=GitBaseVersionDescriptor(
                    version=pr.source_ref_name.replace('refs/heads/', ''),
                    version_type="branch"
                )
            )
            
            # Get the target branch version
            target_content = git_client.get_item_content(
                repository_id=repository_id,
                project=project_name,
                path=file_path,
                download=True,  # Important: This forces content download
                version_descriptor=GitTargetVersionDescriptor(
                    version=pr.target_ref_name.replace('refs/heads/', ''),
                    version_type="branch"
                )
            )
            
            # Convert byte streams to strings
            pr_text = ''.join(chunk.decode('utf-8') for chunk in pr_content)
            target_text = ''.join(chunk.decode('utf-8') for chunk in target_content)
            # Compare the contents
            differ = difflib.Differ()
            diff = differ.compare(pr_text.splitlines(), target_text.splitlines()) 
            actual_changes = [line.lstrip('-+ ')  for line in diff if line.startswith(('- ', '+ '))]
            if actual_changes:
                file_changes = {
                    "file": file_path,
                    "changes": '\n'.join([line if line else '' for line in actual_changes])
                }
                changed_content.append(file_changes)
            else:
                file_changes = None
        except Exception as e:
            print(f"Error processing {file_path}: {str(e)}")
    return changed_content



# Main function to fetch PRs and review them
def review_pull_requests():
    try:
        pull_requests = get_pull_requests()
        for pr in pull_requests:
            author_name = pr.created_by.display_name

            # Ignore PRs by specified authors
            if author_name in IGNORED_AUTHORS:
                print(f"Ignoring PR #{pr.pull_request_id} by {author_name}")
                continue

            print(f"Reviewing PR #{pr.pull_request_id} - {pr.title} by {author_name}")

            # Fetch the diff content for the pull request
            diff = fetch_pr_diff(pr.pull_request_id)
            if diff:
                print(f"Fetched diff content for PR #{pr.pull_request_id}")

                # Analyze the diff content using OpenAI
                review_comment = analyze_pr_diff(diff)
                # Comment on the pull request with the generated feedback
                comment_on_pr(pr.pull_request_id, review_comment)
                print(f"Posted review comment on PR #{pr.pull_request_id}")
            else:
                print(f"No diff content found for PR #{pr.pull_request_id}")
    except Exception as e:
        print(f"Error reviewing pull requests: {str(e)}")

# Run the script
if __name__ == "__main__":
    try:
        while True:
            app.run(port=flask_port)
    except Exception as e:
        print(f"An error occurred while reviewing pull requests: {str(e)}")
