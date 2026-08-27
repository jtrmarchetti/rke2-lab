# Verify harness for the dev.lo cluster end state.
#
# A pytest suite that checks the *result* of the Ansible estate rather than
# re-applying it: SSO flows, observability data, vault contents, storage
# health, and baseline cluster readiness. See docs/source/tasks/
# testing-cluster.rst and plan/TARGETS.md for what is asserted and why.
