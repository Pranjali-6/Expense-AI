"""Seed reference data and, optionally, a demo tenant.

    python -m app.db.seed              reference data only
    python -m app.db.seed --demo       plus a fictional demo tenant

Idempotent: every insert is an upsert keyed on a natural key, so running it
twice changes nothing and running it after adding a merchant adds only that
merchant.

Note how the demo tenant is written. The seed connects as the application role,
which is subject to Row Level Security, so it has to set
``app.current_tenant_id`` before it can insert a single tenant-scoped row. That
is not a workaround — it is the same path the API takes, and it means this
script would fail loudly if the policies were ever broken.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging, get_logger
from app.core.security import account_fingerprint, hash_password
from app.db.session import dispose_engine, get_session_factory
from app.db.seed_data import CATEGORIES, MERCHANTS
from app.models import (
    Account,
    Category,
    Merchant,
    MerchantAlias,
    Subcategory,
    Tenant,
    User,
)
from app.models.enums import AccountStatus, AccountType, AuthProvider, UserRole

logger = get_logger(__name__)

DEMO_TENANT_SLUG = "demo"
# `.local` is a reserved special-use domain and is rejected by email
# validation — a demo account created with one could never sign in.
DEMO_EMAIL = "demo@expense-ai.dev"
DEMO_PASSWORD = "DemoPassword123!"


async def seed_categories(session: AsyncSession) -> dict[str, uuid.UUID]:
    """Upsert the category tree. Returns slug -> id for both levels."""
    ids: dict[str, uuid.UUID] = {}

    for order, seed in enumerate(CATEGORIES):
        stmt = (
            insert(Category)
            .values(
                slug=seed["slug"],
                name=seed["name"],
                is_expense=seed["is_expense"],
                is_income=seed["is_income"],
                is_system=True,
                color=seed["color"],
                icon=seed["icon"],
                sort_order=order,
            )
            .on_conflict_do_update(
                index_elements=[Category.slug],
                set_={
                    "name": seed["name"],
                    "is_expense": seed["is_expense"],
                    "is_income": seed["is_income"],
                    "color": seed["color"],
                    "icon": seed["icon"],
                    "sort_order": order,
                },
            )
            .returning(Category.id)
        )
        category_id = (await session.execute(stmt)).scalar_one()
        ids[seed["slug"]] = category_id

        for sub_order, sub in enumerate(seed["subcategories"]):
            sub_stmt = (
                insert(Subcategory)
                .values(
                    category_id=category_id,
                    slug=sub["slug"],
                    name=sub["name"],
                    sort_order=sub_order,
                )
                .on_conflict_do_update(
                    index_elements=[Subcategory.category_id, Subcategory.slug],
                    set_={"name": sub["name"], "sort_order": sub_order},
                )
                .returning(Subcategory.id)
            )
            sub_id = (await session.execute(sub_stmt)).scalar_one()
            # Namespaced so a subcategory slug can repeat across categories
            # ("books_supplies" under Education, "books_media" under
            # Entertainment) without colliding here.
            ids[f"{seed['slug']}.{sub['slug']}"] = sub_id

    return ids


async def seed_merchants(session: AsyncSession, ids: dict[str, uuid.UUID]) -> int:
    alias_count = 0

    for seed in MERCHANTS:
        category_id = ids.get(seed["category"])
        subcategory_id = (
            ids.get(f"{seed['category']}.{seed['subcategory']}")
            if seed["subcategory"]
            else None
        )

        stmt = (
            insert(Merchant)
            .values(
                slug=seed["slug"],
                display_name=seed["name"],
                category_id=category_id,
                subcategory_id=subcategory_id,
                # Seeded mappings are curated, so they rank as verified in the
                # categorisation cascade — above deterministic rules, below the
                # user's own corrections.
                is_verified=True,
                mcc=seed["mcc"],
                is_subscription_like=seed["subscription"],
            )
            .on_conflict_do_update(
                index_elements=[Merchant.slug],
                set_={
                    "display_name": seed["name"],
                    "category_id": category_id,
                    "subcategory_id": subcategory_id,
                    "is_verified": True,
                    "mcc": seed["mcc"],
                    "is_subscription_like": seed["subscription"],
                },
            )
            .returning(Merchant.id)
        )
        merchant_id = (await session.execute(stmt)).scalar_one()

        for alias in seed["aliases"]:
            normalised = alias.strip().upper()
            # Longer patterns are more specific, so they must win. Priority
            # ascends with generality: "SWIGGY INSTAMART" outranks "SWIGGY".
            priority = max(1, 200 - len(normalised))
            await session.execute(
                insert(MerchantAlias)
                .values(
                    merchant_id=merchant_id,
                    pattern=normalised,
                    match_type="contains",
                    priority=priority,
                )
                .on_conflict_do_update(
                    index_elements=[
                        MerchantAlias.merchant_id,
                        MerchantAlias.pattern,
                        MerchantAlias.match_type,
                    ],
                    set_={"priority": priority},
                )
            )
            alias_count += 1

    return alias_count


async def seed_demo_tenant(session: AsyncSession) -> uuid.UUID | None:
    """Create a fictional demo tenant with two accounts and no transactions.

    Transactions arrive by uploading generated statements in P4 — seeding them
    directly would bypass the pipeline that is supposed to produce them, and a
    ledger that never went through reconciliation is not a useful demo of a
    system whose whole claim is that it reconciles.
    """
    # Through the SECURITY DEFINER function, not a plain SELECT. Row Level
    # Security applies to this connection, so `SELECT ... WHERE slug = 'demo'`
    # returns zero rows whether or not the tenant exists — which made this
    # check silently useless and `make bootstrap` non-idempotent despite the
    # docstring above. See migration 0011.
    existing = await session.execute(
        text("SELECT ops_tenant_id_by_slug(:slug)"), {"slug": DEMO_TENANT_SLUG}
    )
    found = existing.scalar_one_or_none()
    if found:
        logger.info("demo_tenant_exists", tenant_id=str(found))
        return found

    tenant_id = uuid.uuid4()

    # Row Level Security applies to this connection. The GUC has to be set
    # before the tenant row itself can be inserted, because the policy's
    # WITH CHECK compares the new row's id against it.
    await session.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )

    session.add(
        Tenant(id=tenant_id, name="Demo Household", slug=DEMO_TENANT_SLUG, ai_enabled=True)
    )
    await session.flush()

    session.add(
        User(
            tenant_id=tenant_id,
            email=DEMO_EMAIL,
            full_name="Demo User",
            password_hash=hash_password(DEMO_PASSWORD),
            auth_provider=AuthProvider.PASSWORD,
            role=UserRole.OWNER,
            email_verified_at=datetime.now(timezone.utc),
        )
    )

    # Fictional accounts. `account_last4` is the only account-number fragment
    # this system ever holds; the fingerprint is what statements match against.
    today = date.today()
    for bank_code, bank_name, acc_type, last4 in (
        # These last-4s match the accounts the demo statements are drawn on
        # (see tools/statement_generator generate_demo). The pipeline resolves
        # an account from the statement's own bank, type and last 4 — it does
        # not use whatever the seed happened to create — so mismatched numbers
        # here leave two accounts that never receive a single transaction
        # sitting on the Accounts screen forever.
        ("HDFC", "HDFC Bank", AccountType.SAVINGS, "0794"),
        ("ICICI", "ICICI Bank", AccountType.CREDIT_CARD, "1015"),
    ):
        session.add(
            Account(
                tenant_id=tenant_id,
                bank_code=bank_code,
                bank_name=bank_name,
                account_type=acc_type,
                status=AccountStatus.ACTIVE,
                account_last4=last4,
                # The *same* function the pipeline uses to resolve an account
                # from a statement. This was a `uuid5` of the same inputs — a
                # second scheme for one concept — so a seeded account could
                # never be matched by an import, by construction: every demo
                # produced two empty placeholders beside the two real accounts
                # the pipeline made for itself.
                account_fingerprint=account_fingerprint(
                    tenant_id, bank_code, str(acc_type), last4
                ),
                display_name=f"{bank_name} ••••{last4}",
                coverage_start=today - timedelta(days=90),
                coverage_end=today,
            )
        )

    logger.info("demo_tenant_created", tenant_id=str(tenant_id))
    return tenant_id


async def run(with_demo: bool) -> None:
    factory = get_session_factory()

    async with factory() as session:
        async with session.begin():
            ids = await seed_categories(session)
            aliases = await seed_merchants(session, ids)

        categories = await session.scalar(
            select(Category.id).with_only_columns(Category.id).limit(1)
        )
        _ = categories  # touch, so a broken read fails here rather than silently

        logger.info(
            "reference_data_seeded",
            count=len(CATEGORIES),
            component="seed",
        )
        print(
            f"  categories      {len(CATEGORIES)}\n"
            f"  subcategories   {sum(len(c['subcategories']) for c in CATEGORIES)}\n"
            f"  merchants       {len(MERCHANTS)}\n"
            f"  aliases         {aliases}"
        )

    if with_demo:
        # A separate session: the tenant GUC is transaction-scoped, and mixing
        # global reference data with tenant-scoped writes in one transaction
        # would mean the reference data was written under a tenant scope too.
        async with factory() as session:
            async with session.begin():
                tenant_id = await seed_demo_tenant(session)
        if tenant_id:
            print(
                f"\n  demo tenant     {tenant_id}\n"
                f"  demo login      {DEMO_EMAIL} / {DEMO_PASSWORD}\n"
                f"  demo accounts   HDFC savings ••••0794, ICICI card ••••1015\n"
                f"\n  No transactions were seeded. Upload generated statements "
                f"(P4) so the\n  ledger is produced by the real pipeline rather "
                f"than injected around it."
            )

    await dispose_engine()


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Seed reference data.")
    parser.add_argument(
        "--demo", action="store_true", help="also create a fictional demo tenant"
    )
    args = parser.parse_args()

    print("Seeding reference data...")
    asyncio.run(run(args.demo))
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
