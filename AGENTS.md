# AGENTS.md

## Project Standards

* Use the most up to date packages and images unless there are legitimate compatibility or stability concerns
* Follow modern design and tool choices for the infrastructure design
* Use web searches, when available, to conduct research prior to implementation or during troubleshooting; do not rely solely on the AI model's built in knowledge

## Coding Standards

* Automation shall:
    * build the entire target environment, from scratch, to a feature complete state
    * not require any manual intervention to complete
    * be fully idempotent
* Limit comments and only use sparingly in limited situations:
    - Explaining obscure implementations
    - Describing magic numbers/values
    - In code documentation for `BUG:`, `TODO:`, etc
* Automation and code should normally be self explainitory without needing comments
* Keep tests updated to reflect the current expected state and functionality of the target environment

## Development

- Perform research before any implementation or fixes step
- Have tech lead review changes prior to any pulumi destroy + cold build or before committing and pushing the final changes
- Implement tech lead requested changes, if any, after each review step
- Perform pulumi destroy and cold build after implementation and tech lead review
- Do not perform cold or warm build process after final tech lead review if no changes are requested to the automation
- If cold build has issues caused by automation changes, make fixes and validate via warm build until changes work, then restart the cold build process
- Run warm build after any cold build for idempotency validation
- Only commit work after testing passes, documentation is updated, and final tech lead review request 0 changes

## Tech Lead Review

* Second guess all assumptions made in the code
* Ensure all changes follow the project standards and processes; report any deviations
* Double check formatting and logic; identify any issues
* Mark your feedback items as either suggestions or requirements to indicate if the changes must be made in order to pass review

## Testing

- Only add or update tests once code changes are complete, reviewed, and both cold and warm builds complete successfully
- Add new tests when it makes sense, don't add tests just to add tests
- Update existing testing logic when making technical changes that affect the existing tests
- Run test suite after any cold or warm build, or after updating testing logic

## Documentation

- Only add or update documentation once code changes are complete, reviewed, both cold and warm builds complete successfully, and testing passes
- Add new documentation when it makes sense
- Update existing documentation when making technical changes that affect the documentation content

## Overall Workflow

Research -> Develop -> Tech Lead Review -> Cold Build -> Warm Build -> Testing -> Documentation -> Final Tech Lead Review -> Commit and Push

Note: any automation change restarts this workflow from step 1
