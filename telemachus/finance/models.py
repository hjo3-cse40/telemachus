"""Data models and enums for the Telemachus double-entry financial engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
from typing import Any, Dict, List, Optional
import uuid


class AccountType(str, Enum):
    """Core double-entry account categories."""
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"


class RuleType(str, Enum):
    """Classification for recurring rules."""
    FIXED = "fixed"
    VARIABLE = "variable"


class Frequency(str, Enum):
    """Cadence of recurring obligations."""
    MONTHLY = "monthly"
    BIWEEKLY = "biweekly"
    WEEKLY = "weekly"
    ANNUAL = "annual"


class BillStatus(str, Enum):
    """Lifecycle status of a bill instance."""
    SCHEDULED = "scheduled"   # Estimated or scheduled ahead of time
    BILLED = "billed"         # Statement arrived; exact amount confirmed
    PAID = "paid"             # Cleared/paid via linked transaction
    SKIPPED = "skipped"       # Explicitly skipped for this cycle


@dataclass
class Account:
    """An account in the double-entry chart of accounts."""
    id: str
    account_type: AccountType
    currency: str = "USD"
    description: str = ""
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "account_type": self.account_type.value,
            "currency": self.currency,
            "description": self.description,
            "is_active": self.is_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Account:
        return cls(
            id=data["id"],
            account_type=AccountType(data["account_type"]),
            currency=data.get("currency", "USD"),
            description=data.get("description", ""),
            is_active=bool(data.get("is_active", True)),
        )


@dataclass
class Posting:
    """A single leg of a double-entry transaction.
    
    amount_cents is a signed 64-bit integer:
    - Positive for Debits (e.g. increase in Expense or increase in Asset)
    - Negative for Credits (e.g. decrease in Asset, increase in Liability, increase in Income)
    """
    account_id: str
    amount_cents: int
    memo: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "amount_cents": self.amount_cents,
            "memo": self.memo,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Posting:
        return cls(
            account_id=data["account_id"],
            amount_cents=int(data["amount_cents"]),
            memo=data.get("memo"),
        )


@dataclass
class Transaction:
    """A balanced financial transaction with at least two postings."""
    id: str
    date: str  # YYYY-MM-DD
    payee: str
    memo: str = ""
    source: str = "cli"  # 'cli', 'mcp', 'notion_import', 'recurring'
    status: str = "posted"  # 'pending', 'posted', 'cleared'
    postings: List[Posting] = field(default_factory=list)
    idempotency_hash: Optional[str] = None
    created_at: int = 0

    @classmethod
    def create(
        cls,
        date: str,
        payee: str,
        postings: List[Posting],
        memo: str = "",
        source: str = "cli",
        status: str = "posted",
        txn_id: Optional[str] = None,
        created_at: int = 0,
    ) -> Transaction:
        tid = txn_id or str(uuid.uuid4())
        # Compute deterministic idempotency hash if postings are present
        norm_postings = sorted([(p.account_id, p.amount_cents) for p in postings])
        hash_str = f"{date}|{payee.strip().lower()}|{norm_postings}|{memo.strip()}"
        idempotency_hash = hashlib.sha256(hash_str.encode("utf-8")).hexdigest()

        return cls(
            id=tid,
            date=date,
            payee=payee,
            memo=memo,
            source=source,
            status=status,
            postings=postings,
            idempotency_hash=idempotency_hash,
            created_at=created_at,
        )

    def is_balanced(self) -> bool:
        """Returns True if sum of postings in cents equals zero."""
        return sum(p.amount_cents for p in self.postings) == 0

    def balance_offset_cents(self) -> int:
        """Returns the discrepancy if any (should be 0)."""
        return sum(p.amount_cents for p in self.postings)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "date": self.date,
            "payee": self.payee,
            "memo": self.memo,
            "source": self.source,
            "status": self.status,
            "postings": [p.to_dict() for p in self.postings],
            "idempotency_hash": self.idempotency_hash,
            "created_at": self.created_at,
        }


@dataclass
class RecurringRule:
    """Rule defining a recurring obligation or payment."""
    id: str
    name: str
    rule_type: RuleType
    frequency: Frequency
    due_day_of_month: int  # 1 to 31
    base_amount_cents: int  # Fixed amount or baseline estimate
    from_account: str
    to_account: str
    is_autopay: bool = False
    lead_time_days: int = 5
    is_active: bool = True
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "rule_type": self.rule_type.value,
            "frequency": self.frequency.value,
            "due_day_of_month": self.due_day_of_month,
            "base_amount_cents": self.base_amount_cents,
            "from_account": self.from_account,
            "to_account": self.to_account,
            "is_autopay": self.is_autopay,
            "lead_time_days": self.lead_time_days,
            "is_active": self.is_active,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RecurringRule:
        return cls(
            id=data["id"],
            name=data["name"],
            rule_type=RuleType(data["rule_type"]),
            frequency=Frequency(data["frequency"]),
            due_day_of_month=int(data["due_day_of_month"]),
            base_amount_cents=int(data["base_amount_cents"]),
            from_account=data["from_account"],
            to_account=data["to_account"],
            is_autopay=bool(data.get("is_autopay", False)),
            lead_time_days=int(data.get("lead_time_days", 5)),
            is_active=bool(data.get("is_active", True)),
            notes=data.get("notes", ""),
        )


@dataclass
class BillInstance:
    """Specific bill instance for a given billing cycle."""
    id: str
    rule_id: str
    billing_cycle: str  # YYYY-MM
    due_date: str       # YYYY-MM-DD
    estimated_amount_cents: int
    actual_amount_cents: Optional[int] = None
    status: BillStatus = BillStatus.SCHEDULED
    matched_transaction_id: Optional[str] = None
    updated_at: int = 0

    @property
    def effective_amount_cents(self) -> int:
        """Returns confirmed actual amount if available, otherwise estimated."""
        return self.actual_amount_cents if self.actual_amount_cents is not None else self.estimated_amount_cents

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "billing_cycle": self.billing_cycle,
            "due_date": self.due_date,
            "estimated_amount_cents": self.estimated_amount_cents,
            "actual_amount_cents": self.actual_amount_cents,
            "status": self.status.value,
            "matched_transaction_id": self.matched_transaction_id,
            "updated_at": self.updated_at,
        }


@dataclass
class CashFlowSummary:
    """Pre-computed deterministic cash flow rollup for a period."""
    start_date: str
    end_date: str
    inflow_cents: int
    outflow_cents: int
    net_savings_cents: int
    savings_rate_pct: float
    category_breakdown: Dict[str, int]
    daily_burn_rate_cents: int
    evaluated_days: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "inflow_cents": self.inflow_cents,
            "outflow_cents": self.outflow_cents,
            "net_savings_cents": self.net_savings_cents,
            "savings_rate_pct": self.savings_rate_pct,
            "category_breakdown": self.category_breakdown,
            "daily_burn_rate_cents": self.daily_burn_rate_cents,
            "evaluated_days": self.evaluated_days,
        }


# ==============================================================================
# Default Chart of Accounts Tailored to User Setup
# ==============================================================================
DEFAULT_USER_ACCOUNTS: List[Account] = [
    # Assets
    Account("Assets:Checking:WellsFargo", AccountType.ASSET, description="Primary Wells Fargo operating account"),
    Account("Assets:Savings:CapitalOne", AccountType.ASSET, description="Capital One High Yield Savings Account"),

    # Liabilities
    Account("Liabilities:CreditCard:Visa", AccountType.LIABILITY, description="Primary Visa credit card for spending"),

    # Income
    Account("Income:Salary", AccountType.INCOME, description="Bi-weekly payroll compensation"),

    # Expenses
    Account("Expenses:Housing:Rent", AccountType.EXPENSE, description="Apartment rent (placeholder $0; dad pays until year ends)"),
    Account("Expenses:Utilities:PGE", AccountType.EXPENSE, description="PG&E electric and gas (variable monthly)"),
    Account("Expenses:Utilities:Internet", AccountType.EXPENSE, description="Home internet (fixed monthly)"),
    Account("Expenses:Food:Groceries", AccountType.EXPENSE, description="Groceries and pantry (variable consistent band)"),
    Account("Expenses:Food:Dining", AccountType.EXPENSE, description="Eating out, restaurants, takeout (discretionary)"),
    Account("Expenses:Personal:Discretionary", AccountType.EXPENSE, description="Leftover personal spending"),
    Account("Expenses:Family:Brother", AccountType.EXPENSE, description="Recurring monthly payment to brother"),
    Account("Expenses:Subscriptions:Gym", AccountType.EXPENSE, description="Gym membership subscription"),
    Account("Expenses:Subscriptions:AppleCloud", AccountType.EXPENSE, description="Apple Cloud storage subscription"),
    Account("Expenses:Subscriptions:Spotify", AccountType.EXPENSE, description="Spotify music subscription"),

    # Equity
    Account("Equity:OpeningBalances", AccountType.EQUITY, description="Double-entry reconciliation initial balance"),
]

# ==============================================================================
# Default Recurring Rules Tailored to User Setup
# ==============================================================================
DEFAULT_USER_RECURRING_RULES: List[RecurringRule] = [
    RecurringRule(
        id="rule_rent",
        name="Apartment Rent",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=1,
        base_amount_cents=0,  # $0 placeholder right now; dad pays until year end
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Housing:Rent",
        is_autopay=False,
        is_active=False,  # Inactive for now
        notes="Currently paid by dad; user starts paying after this year ends.",
    ),
    RecurringRule(
        id="rule_pge",
        name="PG&E Electric & Gas",
        rule_type=RuleType.VARIABLE,
        frequency=Frequency.MONTHLY,
        due_day_of_month=15,  # Typical mid-month utility due date
        base_amount_cents=9000,  # $90.00 baseline estimate before history
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Utilities:PGE",
        is_autopay=True,
        lead_time_days=7,
        is_active=True,
        notes="Variable utility bill; engine computes rolling moving average + seasonal buffer.",
    ),
    RecurringRule(
        id="rule_internet",
        name="Internet",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=5,
        base_amount_cents=5000,  # $50.00 baseline fixed
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Utilities:Internet",
        is_autopay=True,
        is_active=True,
        notes="Fixed monthly internet bill.",
    ),
    RecurringRule(
        id="rule_brother",
        name="Payment to Brother",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=1,
        base_amount_cents=0,  # User will set exact amount or update
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Family:Brother",
        is_autopay=False,
        is_active=True,
        notes="Fixed recurring payment to brother.",
    ),
    RecurringRule(
        id="rule_gym",
        name="Gym Membership",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=10,
        base_amount_cents=4500,  # $45.00 baseline
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:Gym",
        is_autopay=True,
        is_active=True,
        notes="Fixed monthly gym subscription.",
    ),
    RecurringRule(
        id="rule_apple_cloud",
        name="Apple Cloud",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=25,
        base_amount_cents=299,  # $2.99 baseline (very low price)
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:AppleCloud",
        is_autopay=True,
        is_active=True,
        notes="Fixed low price Apple Cloud subscription.",
    ),
    RecurringRule(
        id="rule_spotify",
        name="Spotify",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=18,
        base_amount_cents=1199,  # $11.99 baseline
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:Spotify",
        is_autopay=True,
        is_active=True,
        notes="Fixed monthly Spotify subscription.",
    ),
]
