# AGENTS.md

## Project standards

* Use the most up to date packages and images unless there are legitimate compatibility or stability concerns
* Follow modern design and tool choices for the infrastructure design
* Use web searches, when available, to conduct research prior to implementation or during troubleshooting; do not rely solely on the AI model's built in knowledge

## Coding standards

* Automation shall:
    * build the entire targeet environment, from scratch, to a feature complete state
    * not require any manual intervention to complete
    * be fully idempotent
* Limit comments and only use sparingly in limited situations:
    - Explaining obscure implementations
    - Describing magic numbers/values
    - In code documentation for `BUG:`, `TODO:`, etc
* Automation and code should normally be self explainitory without needing comments
* Keep tests updated to reflect the current expected state and functionality of the target environment

## Development process loop

* Any changes or fixes to the IaC or configuration as code automation logic require starting the loop from step 1 again
* Use the `.agents/skills/infra-agent/SKILL.md` skill

1. Research any additional required changes
2. Implement any additional required changes
3. Have Tech Lead review changes made since last review; restart loop if feedback requires changes
4. Destroy/build IaC stack; restart loop if you encounter failures that require changes
5. Run cold build of configuration as code automation; fix issues and rerun automation until no failures, then restart loop
6. Verify IaC and configuration as code automation build idempotency via warm build; fix issues and rerun automation until no failures, then restart loop
7. Run tests; fix any test logic bugs and rerun until all tests function
8. Add or update new tests as needed; run and update test logic until they all function
9. Fix any IaC or configuration as code automation issues until all tests pass; restart loop when complete or if a full rebuild is needed to pass tests
10. Review the docs; make any necessary updates based on the project changes and validate docs build successfully
11. Have Tech Lead perform final review and make any requested changes
12. Commit and push changes

## Tech Lead review process

* Use the `.agents/skills/tech-lead-reviewer/SKILL.md` skill
* Second guess all assumptions made in the code
* Ensure all changes follow the project standards and processes; report any deviations
* Double check formatting and logic; identify any issues
