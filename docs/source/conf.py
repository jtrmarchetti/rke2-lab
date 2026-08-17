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

templates_path = ["_templates"]
exclude_patterns = ["_build"]

# Warnings are errors in the Makefile (-W), so a broken cross-reference or an
# orphaned page fails the build rather than shipping.
nitpicky = False
todo_include_todos = True

html_theme = "furo"
html_title = "dev.lo Sysadmin Guide"
html_static_path = ["_static"]
html_show_sourcelink = True

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
