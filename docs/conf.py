from pathlib import Path

project = "spires-batch"
author = "RittgerLabGroup"
extensions = ["myst_parser"]
root_doc = "docs/README"
include_patterns = ["README.md", "docs/*.md", "docs/**/*.md", "examples/*.md"]
exclude_patterns = ["docs/_build/**"]
html_theme = "pydata_sphinx_theme"
templates_path = ["_templates"]
html_additional_pages = {"index": "landing.html"}
html_static_path = ["_static"]
html_logo = "_static/logo.svg"
myst_heading_anchors = 6

def setup(app):
    def include_navigation(application):
        root = Path(application.srcdir)
        documents = sorted(
            path.relative_to(root).with_suffix("").as_posix()
            for directory in ("docs", "examples")
            for path in (root / directory).rglob("*.md")
            if "_build" not in path.parts and path != root / "docs/README.md"
        )
        application.config.html_context["documentation_pages"] = documents

    def append_navigation(application, docname, source):
        if docname == "docs/README":
            documents = application.config.html_context["documentation_pages"]
            source[0] += "\n\n```{toctree}\n:hidden:\n\n/README\n"
            source[0] += "\n".join("/" + name for name in documents)
            source[0] += "\n```\n"

    app.connect("builder-inited", include_navigation)
    app.connect("source-read", append_navigation)
