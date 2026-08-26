# Build the environment guide (sysadmin + developer sections).
#
#   python3 -m venv ~/.venvs/rke2lab-docs
#   ~/.venvs/rke2lab-docs/bin/pip install -r docs/requirements.txt
#   make -C docs html
#
# The virtual environment is the guide's own, separate from ~/.venvs/rke2lab
# for the same reason infra/pulumi keeps its own: they pin different things for
# different reasons.

SPHINXBUILD ?= $(HOME)/.venvs/rke2lab-docs/bin/sphinx-build
SOURCEDIR   = source
BUILDDIR    = _build

.PHONY: html linkcheck clean help

help:
	@echo "html       build the HTML site into $(BUILDDIR)/html"
	@echo "linkcheck  check external links (needs internet)"
	@echo "clean      remove $(BUILDDIR)"

html:
	$(SPHINXBUILD) -b html -W --keep-going "$(SOURCEDIR)" "$(BUILDDIR)/html"

linkcheck:
	$(SPHINXBUILD) -b linkcheck "$(SOURCEDIR)" "$(BUILDDIR)/linkcheck"

clean:
	rm -rf "$(BUILDDIR)"
