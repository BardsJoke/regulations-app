
import json
from collections import Counter
from pathlib import Path


SOURCE_FILE = Path("sqlite_before_postgresql.json")
OUTPUT_FILE = Path("postgresql_users_roles_profiles.json")

ALLOWED_MODELS = {
    "accounts.user",
    "access.roleassignment",
    "wagtailusers.userprofile",
}

EXPECTED_COUNTS = {
    "accounts.user": 3,
    "access.roleassignment": 3,
    "wagtailusers.userprofile": 1,
}


def main():
    if not SOURCE_FILE.is_file():
        raise SystemExit(
            f"Source file not found: {SOURCE_FILE}"
        )

    source_data = json.loads(
        SOURCE_FILE.read_text(encoding="utf-8")
    )

    selected = [
        item
        for item in source_data
        if item["model"] in ALLOWED_MODELS
    ]

    counts = Counter(
        item["model"]
        for item in selected
    )

    print("SOURCE RECORDS:", len(source_data))
    print("SELECTED RECORDS:", len(selected))

    for model, expected in EXPECTED_COUNTS.items():
        actual = counts.get(model, 0)

        print(f"{model}: {actual}")

        if actual != expected:
            raise SystemExit(
                f"Unexpected record count for {model}: "
                f"expected {expected}, got {actual}"
            )

    # Пользователи должны идти первыми,
    # так как на них ссылаются остальные записи.
    order = {
        "accounts.user": 0,
        "access.roleassignment": 1,
        "wagtailusers.userprofile": 2,
    }

    selected.sort(
        key=lambda item: order[item["model"]]
    )

    usernames = {
        item["fields"]["username"]
        for item in selected
        if item["model"] == "accounts.user"
    }

    # Проверяем ссылки назначений ролей.
    for item in selected:
        if item["model"] == "access.roleassignment":
            user_ref = item["fields"]["user"]

            if (
                not isinstance(user_ref, list)
                or len(user_ref) != 1
                or user_ref[0] not in usernames
            ):
                raise SystemExit(
                    "Invalid user reference in RoleAssignment."
                )

        if item["model"] == "wagtailusers.userprofile":
            user_ref = item["fields"]["user"]

            if (
                not isinstance(user_ref, list)
                or len(user_ref) != 1
                or user_ref[0] not in usernames
            ):
                raise SystemExit(
                    "Invalid user reference in UserProfile."
                )

    OUTPUT_FILE.write_text(
        json.dumps(
            selected,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print()
    print("IMPORT FIXTURE CREATED SUCCESSFULLY")
    print("File:", OUTPUT_FILE)
    print("Records:", len(selected))

    # Показываем только ссылки на разрешения,
    # не выводим хеши паролей или другие секреты.
    for item in selected:
        if item["model"] == "accounts.user":
            permissions = item["fields"].get(
                "user_permissions",
                [],
            )

            if permissions:
                print(
                    "User permission references:",
                    json.dumps(
                        permissions,
                        ensure_ascii=True,
                    ),
                )


if __name__ == "__main__":
    main()
