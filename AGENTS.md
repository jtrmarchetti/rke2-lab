# AGENTS.md

## Tech Lead Review
### Perform code review


### Perform Documentation scrub
Check that documentation reflects changes that new automation updates make

### Performn comment scrub
Check that any new comments meet the coding standards

### Perform test scrub
Check that tests reflect changes that new automation updates make

## Coding standards
* Automation shall build from stratch the entire targeet environment to a feature complete state
* Automation shall execute fully from start to finish with no manual intervention
* Automation shall be fully idempotent
* Limit comments and only use sparingly in limited situations:
    - Explaining obscure implementations
    - Describing magic numbers/values
    - In code documentation for `BUG:`, `TODO:`, etc
* Automation and code should normally be self explainitory without needing comments
* Keep tests updated to reflect the current expected state and functionality of the target environment



## Development process loop
* Any changes or fixes to the IaC or configuration as code automation logic requires starting the loop from step 1 again

1. Research any additional required changes
2. Implement any additioanl required changes
3. Have Tech Lead review changes made since last review; restart loop if feedback requires changes
4. Destroy/build IaC stack; restart loop if you encounter failures that require changes
5. Run cold build of configuration as code automation; fix issues and rerun automation until no failures, then restart loop
6. Verify IaC and configuration as code automation build idempotency via warm build; fix issues and rerun automation until no failures, then restart loop
7. Run tests; fix any test logic bugs and rerun until all tests function
8. Add or update new tests as needed; run and update test logic until they all function
9. Fix any IaC or configuration as code automation issues until all tests pass; restart loop when complete or if a full rebuild is needed to pass tests
10. Have Tech Lead perform final review and make any requested changes
11. Commit changes
