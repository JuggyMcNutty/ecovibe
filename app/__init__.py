"""ECOVibe application package.

The FastAPI app instance is constructed lazily in `app.main.create_app()`
and exposed as `app.main.app`. Import `app.main` explicitly rather than
importing `app` directly to avoid triggering app construction during tests
that only need a submodule.
"""
