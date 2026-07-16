import os

from app import create_app, db

app = create_app()

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    # Debug stays on by default for local use; set FLASK_DEBUG=0 if this ever
    # runs anywhere other than your own machine (the debugger allows code execution).
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
