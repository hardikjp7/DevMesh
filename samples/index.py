from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# 1. Structure the mock output payload received from your AI agent
ai_review_data = {
    "repo_name": "secure-payment-gateway",
    "pr_number": 104,
    "health_score": 82,
    "findings": [
        {
            "severity": "CRITICAL",
            "title": "SQL Injection Vulnerability",
            "file_path": "auth/login.py",
            "line": 42,
            "color": "#EF4444", # Red
            "description": "The custom login query string combines raw parameters without serialization. An attacker can hijack database assertions.",
            "suggested_code": "cursor.execute(\"SELECT * FROM users WHERE user = %s\", (username,))"
        },
        {
            "severity": "WARNING",
            "title": "Unoptimized O(N^2) Iteration Loop",
            "file_path": "utils/helpers.py",
            "line": 118,
            "color": "#F59E0B", # Orange
            "description": "Nested item lookup causes execution lag across large arrays. Convert list searches to set hashing.",
            "suggested_code": "seen_lookup = set(registered_ids)\nif item_id in seen_lookup:\n    process_item()"
        }
    ]
}

# 2. Render HTML via Jinja2
env = Environment(loader=FileSystemLoader('.'))
template = env.get_template('report_template.html')
rendered_html_string = template.render(ai_review_data)

# 3. Compile cleanly into your final styled PDF
print("Compiling UI layouts into final report artifact...")
HTML(string=rendered_html_string, base_url='.').write_pdf('ai_code_review_report.pdf')
print("Successfully generated: ai_code_review_report.pdf")
