class RevisionConflictError(Exception):
    def __init__(
        self,
        expected_active_revision: int,
        actual_active_revision: int,
    ) -> None:
        self.expected_active_revision = (
            expected_active_revision
        )

        self.actual_active_revision = (
            actual_active_revision
        )

        super().__init__(
            "Revision conflict. The active revision "
            "changed before the request was processed."
        )


def validate_expected_active_revision(
    value,
) -> int:
    if isinstance(value, bool):
        raise ValueError(
            "expected_active_revision must be "
            "an integer."
        )

    if not isinstance(value, int):
        raise ValueError(
            "expected_active_revision must be "
            "an integer."
        )

    if value < 1:
        raise ValueError(
            "expected_active_revision must be "
            "greater than or equal to 1."
        )

    return value


def ensure_expected_active_revision(
    current_record: dict,
    expected_active_revision: int,
) -> None:
    expected_revision = (
        validate_expected_active_revision(
            expected_active_revision
        )
    )

    actual_revision = current_record[
        "active_revision"
    ][
        "revision_number"
    ]

    if not isinstance(
        actual_revision,
        int,
    ):
        raise ValueError(
            "The current record active revision "
            "must be an integer."
        )

    if expected_revision != actual_revision:
        raise RevisionConflictError(
            expected_active_revision=(
                expected_revision
            ),
            actual_active_revision=(
                actual_revision
            ),
        )