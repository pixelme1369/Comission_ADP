"""One-off CLI to create the first admin login (or any agent account) directly
against DATABASE_URL. There's no signup page by design — run this locally,
pointed at the same DATABASE_URL Vercel uses, once.

Usage:
    export DATABASE_URL="postgresql://...neon connection string..."
    python create_admin.py
"""

import getpass
import sys

from agent_portal import create_app, db
from agent_portal.models import Agent, AgentAlias


def main():
    app = create_app()

    with app.app_context():
        db.create_all()  # no-op if tables already exist (e.g. Vercel already ran a cold start)

        email = input("Email: ").strip().lower()
        if Agent.query.filter_by(email=email).first():
            print(f"An account with email {email} already exists.")
            sys.exit(1)

        display_name = input("Display name: ").strip()
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.")
            sys.exit(1)

        is_admin = input("Admin account? [y/N]: ").strip().lower() == "y"
        agent_name = input("CRM \"Sales Rep\" name to map (optional, press Enter to skip): ").strip()

        agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
        agent.set_password(password)
        db.session.add(agent)
        db.session.flush()
        if agent_name:
            db.session.add(AgentAlias(agent_id=agent.id, agent_name=agent_name))
        db.session.commit()

        print(f"Created {'admin' if is_admin else 'agent'} account for {display_name} ({email}).")


if __name__ == "__main__":
    main()
