"""Creates the first administrator, asking for the password so it is never logged or stored in a file.

    python -m app.create_admin
    docker compose exec app python -m app.create_admin

The address comes from ADMIN_EMAIL (or --email). It refuses to run if any user exists.
"""
import argparse
import getpass
import sys

from . import security
from .config import settings
from .db import Base, SessionLocal, User, engine

MIN_LENGTH = security.MIN_PASSWORD


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default=settings.admin_email, help="sign-in address (default: ADMIN_EMAIL)")
    email = ap.parse_args().email.lower()

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if db.query(User).count():
            sys.exit("Users already exist. Add more on the Users page, or reset a password there.")
        password = getpass.getpass(f"Password for {email} (at least {MIN_LENGTH} characters): ")
        if len(password) < MIN_LENGTH:
            sys.exit(f"The password must be at least {MIN_LENGTH} characters.")
        if getpass.getpass("Repeat the password: ") != password:
            sys.exit("The passwords do not match.")
        db.add(User(email=email, name="Administrator", role="admin",
                    password_hash=security.hash_password(password), must_change=False))
        db.commit()
    print(f"Administrator {email} created. Sign in and turn on two-step sign-in under Account.")


if __name__ == "__main__":
    main()
