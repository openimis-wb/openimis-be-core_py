import datetime
import uuid
from collections import Counter

from django.test import TestCase

from core.models import Role, RoleRight
from core.schema import duplicate_role, update_or_create_role


def _role(**extra):
    return Role.objects.create(
        name=f"Role {uuid.uuid4().hex[:8]}",
        is_system=0,
        is_blocked=False,
        audit_user_id=-1,
        validity_from=datetime.datetime.now(),
        **extra,
    )


def _right(role, right_id, *, closed=False):
    return RoleRight.objects.create(
        role=role,
        right_id=right_id,
        audit_user_id=-1,
        validity_from=datetime.datetime(2026, 1, 1),
        validity_to=datetime.datetime(2026, 2, 1) if closed else None,
    )


def _live(role):
    return Counter(
        RoleRight.objects.filter(role=role, validity_to__isnull=True)
        .values_list("right_id", flat=True)
    )


class DuplicateRoleTest(TestCase):
    def test_duplicate_without_rights_id_copies_only_live_rows(self):
        source = _role()
        _right(source, 9001)
        _right(source, 9002, closed=True)
        _right(source, 9003)
        _right(source, 9003, closed=True)

        copy = duplicate_role({"uuid": source.uuid, "name": "Copy"}, None)

        self.assertEqual(_live(copy), Counter({9001: 1, 9003: 1}))
        self.assertEqual(
            RoleRight.objects.filter(role=copy, right_id=9003).count(), 1
        )
        self.assertFalse(RoleRight.objects.filter(role=copy, right_id=9002).exists())

    def test_duplicate_with_a_repeated_right_id_writes_one_live_row(self):
        source = _role()
        _right(source, 9001)

        copy = duplicate_role(
            {"uuid": source.uuid, "name": "Copy", "rights_id": [9001, 9002, 9001, "9002"]},
            None,
        )

        self.assertEqual(_live(copy), Counter({9001: 1, 9002: 1}))
        self.assertEqual(RoleRight.objects.filter(role=copy).count(), 2)

    def test_duplicate_leaves_the_source_untouched(self):
        source = _role()
        _right(source, 9001)
        _right(source, 9002, closed=True)

        duplicate_role({"uuid": source.uuid, "name": "Copy"}, None)

        self.assertEqual(RoleRight.objects.filter(role=source).count(), 2)
        self.assertEqual(_live(source), Counter({9001: 1}))


class UpdateOrCreateRoleTest(TestCase):
    def test_update_with_a_repeated_right_id_writes_one_live_row(self):
        role = _role()

        update_or_create_role({"uuid": role.uuid, "rights_id": [9001, 9001, 9002]}, None)

        self.assertEqual(_live(role), Counter({9001: 1, 9002: 1}))
        self.assertEqual(RoleRight.objects.filter(role=role).count(), 2)

    def test_update_treats_a_numeric_string_as_the_same_right(self):
        role = _role()

        update_or_create_role({"uuid": role.uuid, "rights_id": [9001, "9001"]}, None)

        self.assertEqual(RoleRight.objects.filter(role=role).count(), 1)

    def test_update_reopens_a_right_the_role_already_held(self):
        role = _role()
        _right(role, 9001)

        update_or_create_role({"uuid": role.uuid, "rights_id": [9001, 9001, 9002]}, None)

        self.assertEqual(_live(role), Counter({9001: 1, 9002: 1}))
        self.assertEqual(RoleRight.objects.filter(role=role, right_id=9001).count(), 1)

    def test_create_with_a_repeated_right_id_writes_one_live_row(self):
        role = update_or_create_role(
            {
                "name": f"Role {uuid.uuid4().hex[:8]}",
                "is_system": 0,
                "is_blocked": False,
                "audit_user_id": -1,
                "validity_from": datetime.datetime.now(),
                "rights_id": [9001, 9002, 9001],
            },
            None,
        )

        self.assertEqual(_live(role), Counter({9001: 1, 9002: 1}))
        self.assertEqual(RoleRight.objects.filter(role=role).count(), 2)
