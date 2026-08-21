"""Move favourites and play counts off the pre-auth placeholder user id.

Before login was delegated to Jellyfin, every row was written under
db.DEFAULT_USER_ID. Now that requests carry a real Jellyfin user id, those rows
belong to nobody and your likes and play counts look like they vanished.

Run once, after your first successful login through the proxy:

    python remap_user.py --list                       # whose rows exist
    python remap_user.py <your-jellyfin-user-id>      # move them
    python remap_user.py <id> --from <other-old-id>   # non-default source

This is deliberately a manual step rather than something that fires on first
login, because auto-remapping would assume whoever logs in first owns the rows -
fine for one user, wrong the moment there are two.
"""

import argparse
import sys

import db


def summarise() -> None:
    conn = db.get_conn()
    print(f"db: {db.DB_PATH}\n")
    for table in ("favorites", "play_counts"):
        rows = conn.execute(
            f"SELECT user_id, COUNT(*) AS n FROM {table} GROUP BY user_id ORDER BY n DESC"
        ).fetchall()
        print(f"  {table}:")
        if not rows:
            print("    (empty)")
        for row in rows:
            marker = "  <- placeholder" if row["user_id"] == db.DEFAULT_USER_ID else ""
            print(f"    {row['user_id']}  {row['n']} rows{marker}")
    print("\n  sessions:")
    for row in conn.execute(
        "SELECT user_id, user_name, MAX(expires_at) AS until FROM sessions "
        "GROUP BY user_id, user_name"
    ).fetchall():
        print(f"    {row['user_id']}  {row['user_name']!r}  valid until {row['until']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("new_user_id", nargs="?",
                        help="the Jellyfin user id to move the rows onto")
    parser.add_argument("--from", dest="old_user_id", default=db.DEFAULT_USER_ID,
                        help=f"source user id (default: {db.DEFAULT_USER_ID})")
    parser.add_argument("--list", action="store_true",
                        help="show which user ids own rows, then exit")
    args = parser.parse_args()

    if args.list or not args.new_user_id:
        summarise()
        if not args.new_user_id:
            print("\nPass a target user id to move the rows.")
        return 0

    if args.new_user_id == args.old_user_id:
        print("source and target are the same id; nothing to do")
        return 1

    print(f"db: {db.DB_PATH}")
    print(f"moving rows from {args.old_user_id} -> {args.new_user_id}")
    moved = db.remap_user_id(args.old_user_id, args.new_user_id)
    for table, count in moved.items():
        print(f"  {table}: {count} rows moved")
    if not any(moved.values()):
        print("  (nothing matched that source id - check `--list`)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
