"""Sphinx configuration for the dev.lo sysadmin guide.

The guide documents a running environment, so it is deliberately free of
autodoc and of anything that has to reach the network to build: it must build
on the automation controller with no internet, from a checkout and nothing
else.
"""

project = "dev.lo RKE2 Sysadmin Guide"
author = "dev.lo platform automation"
copyright = "dev.lo lab"

# The environment this guide describes, not a version of the guide itself.
# Bump it when the cluster's Kubernetes minor version changes.
release = "RKE2 v1.35.7+rke2r1"
version = release

extensions = [
    "sphinx.ext.todo",
]

exclude_patterns = ["_build"]

# Warnings are errors in the Makefile (-W), so a broken cross-reference or an
# orphaned page fails the build rather than shipping.
nitpicky = False
todo_include_todos = True

html_theme = "furo"
html_title = "dev.lo Sysadmin Guide"
html_show_sourcelink = True

# No html_static_path and no templates_path: this guide overrides nothing in
# the theme, and both directories would be empty. Git does not track an empty
# directory, so declaring them builds locally and fails in CI with
# "html_static_path entry '_static' does not exist" — which is what happened on
# the first Pages run. Add the setting back in the same change that adds a file
# to the directory, never before.

html_theme_options = {
    "navigation_with_keys": True,
    "sidebar_hide_name": False,
}

# Sensible defaults for a guide that is mostly shell.
highlight_language = "console"
pygments_style = "friendly"
pygments_dark_style = "native"

rst_prolog = """
.. |domain| replace:: ``dev.lo``
.. |cdomain| replace:: ``k8s.dev.lo``
"""
