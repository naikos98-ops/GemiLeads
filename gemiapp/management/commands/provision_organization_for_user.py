"""Operator-run: give one existing customer their Organization (compatibility layer).

    python manage.py provision_organization_for_user <user id | username | email> [--name "…"] [--dry-run]

Creates, through ``gemiapp.organizations.create_organization`` (so every invariant of the domain service holds), one
``Organization`` with its ``OrganizationProfile`` and the user's ``OrganizationMember`` with role owner. Nothing
else: no subscription, Stripe, legacy Radar, lead, match, organization Radar or opportunity is created, copied or
changed. Billing stays user-owned; the organization's entitlement is derived from this owner's existing
subscription (``gemiapp.organization_entitlement``) and is reported, never granted.

Idempotent: a user who already owns exactly one organization is reported and left as is. Refused, with nothing
written: an unknown or ambiguous identifier, an inactive account, a user who already owns several organizations
(malformed) or who is a non-owner member elsewhere (ambiguous tenancy). Organizations are never created
automatically -- not on signup, login, migration or page visit -- only by an operator running this command.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from gemiapp.models import OrganizationMember
from gemiapp.organization_entitlement import resolve_organization_entitlement
from gemiapp.organizations import OrganizationError, create_organization


def _find_user(identifier: str) -> User:
    identifier = (identifier or "").strip()
    if not identifier:
        raise CommandError("A user identifier (id, username or email) is required.")
    if identifier.isascii() and identifier.isdigit():
        matches = list(User.objects.filter(pk=int(identifier)))
    else:
        matches = list(User.objects.filter(Q(username=identifier) | Q(email__iexact=identifier)).distinct())
    if not matches:
        raise CommandError(f"No user matches {identifier!r}.")
    if len(matches) > 1:
        raise CommandError(f"{identifier!r} matches {len(matches)} users; use the numeric user id instead.")
    return matches[0]


def _default_name(user: User) -> str:
    return f"{user.first_name} {user.last_name}".strip() or user.email or user.username


class Command(BaseCommand):
    help = "Create one Organization (with its owner membership) for an existing user. Operator-run and idempotent."

    def add_arguments(self, parser):
        parser.add_argument("user", help="user id, username or email of the existing customer")
        parser.add_argument("--name", help="organization name (default: the user's full name, else their email)")
        parser.add_argument("--dry-run", action="store_true", help="report what would happen and write nothing")

    def handle(self, *args, **options):
        with transaction.atomic():
            user = _find_user(options["user"])
            # Serialize concurrent runs for the same user, so two operators cannot create two organizations.
            user = User.objects.select_for_update().get(pk=user.pk)
            if not user.is_active:
                raise CommandError(f"User #{user.pk} is inactive; refusing to provision.")
            memberships = list(OrganizationMember.objects.filter(user=user).select_related("organization")
                               .order_by("pk"))
            owned = [m for m in memberships if m.role == OrganizationMember.OWNER]
            other = [m for m in memberships if m.role != OrganizationMember.OWNER]
            if len(owned) > 1:
                raise CommandError(f"User #{user.pk} already owns {len(owned)} organizations "
                                   f"({', '.join(str(m.organization_id) for m in owned)}); refusing an ambiguous state.")
            if other:
                raise CommandError(f"User #{user.pk} is a non-owner member of organization(s) "
                                   f"{', '.join(str(m.organization_id) for m in other)}; refusing an ambiguous state.")
            if owned:
                organization = owned[0].organization
                self._report("unchanged: already owns", organization)
                return
            name = (options.get("name") or _default_name(user)).strip()
            if options["dry_run"]:
                self.stdout.write(f"dry run: would create organization {name!r} with user #{user.pk} as owner; "
                                  "nothing written.")
                return
            try:
                organization = create_organization(owner=user, name=name).organization
            except OrganizationError as error:
                raise CommandError(f"Refused by the organization service: {error}") from error
        self._report("created", organization)

    def _report(self, action, organization):
        entitlement = resolve_organization_entitlement(organization)
        access = (f"entitled ({entitlement.effective_tier})" if entitlement.entitled
                  else f"NOT entitled ({entitlement.reason}): its pages show the paywall until the owner subscribes")
        self.stdout.write(self.style.SUCCESS(f"{action} organization #{organization.pk} {organization.name!r}") +
                          f" | owner user #{entitlement.owner_user_id} | 2.0 access: {access}")
        self.stdout.write("Billing unchanged (user-owned). No Radar, lead, match or opportunity was copied or created.")
