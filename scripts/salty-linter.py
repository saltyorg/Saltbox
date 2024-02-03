import os
import sys

def lint_ansible_defaults(content, file_path):
    errors = []
    lines = content.split('\n')
    
    multi_line_jinja_start = None
    within_multi_line_jinja = False

    for line_no, line in enumerate(lines, start=1):
        stripped_line = line.strip()

        if stripped_line == '---':
            continue

        if '{{' in stripped_line and not within_multi_line_jinja:
            multi_line_jinja_start = line.find('{{')
            within_multi_line_jinja = '}}' not in stripped_line

        elif within_multi_line_jinja:
            if 'if' in stripped_line or 'else' in stripped_line:
                if line.find('if') < multi_line_jinja_start and line.find('else') < multi_line_jinja_start:
                    message = f"'if/else' within Jinja expression should align with the start."
                    errors.append((line_no, message))

            if '}}' in stripped_line:
                within_multi_line_jinja = False

        if '}}' in stripped_line and within_multi_line_jinja:
            within_multi_line_jinja = False

    for error in errors:
        line_no, message = error
        print(f"::warning file={file_path},line={line_no},endLine={line_no},title=Salty Lint Error::{message}")

    return len(errors) > 0

def crawl_and_lint_ansible_roles(roles_dir):
    errors_found = False

    if not os.path.exists(roles_dir):
        print("Roles directory does not exist.")
        return

    for role_name in os.listdir(roles_dir):
        defaults_main_path = os.path.join(roles_dir, role_name, "defaults", "main.yml")
        if os.path.isfile(defaults_main_path):
            with open(defaults_main_path, 'r') as file:
                content = file.read()
                if lint_ansible_defaults(content, defaults_main_path):
                    errors_found = True

    sys.exit(1 if errors_found else 0)

if len(sys.argv) < 2:
    print("Usage: python script.py /path/to/your/ansible/roles")
    sys.exit(1)

roles_directory_path = sys.argv[1]
crawl_and_lint_ansible_roles(roles_directory_path)
