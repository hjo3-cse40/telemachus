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


@dataclass
class MonthlyOverview:
    """One row per month matching user's exact Notion Monthly Overview model and formulas.
    
    Formula Invariants:
    - all_bills = fixed_bills + pge
    - groceries_left = groceries_planned - groceries_actual
    - gas_left = gas_planned - gas_actual
    - variable_variance = (groceries_planned - groceries_actual) + (gas_planned - gas_actual)
    - total_personal = income - savings - fixed_bills - pge - groceries_planned - gas_planned
    - remaining_cushion = income - savings - total_personal - fixed_bills - pge - groceries_actual - gas_actual
    """
    month: str  # YYYY-MM e.g. "2026-10"
    status: str = "open"  # 'open' while month is active, 'closed' when PGE & actuals final
    income_cents: int = 0
    savings_target_cents: int = 0
    fixed_bills_cents: int = 26071  # Default $260.71 matching user's Notion sum
    pge_cents: int = 0
    groceries_planned_cents: int = 0
    groceries_actual_cents: int = 0
    dining_planned_cents: int = 0
    dining_actual_cents: int = 0
    gas_planned_cents: int = 0
    gas_actual_cents: int = 0
    notes: str = ""
    updated_at: int = 0

    @property
    def all_bills_cents(self) -> int:
        return self.fixed_bills_cents + self.pge_cents

    @property
    def groceries_left_cents(self) -> int:
        return self.groceries_planned_cents - self.groceries_actual_cents

    @property
    def dining_left_cents(self) -> int:
        return self.dining_planned_cents - self.dining_actual_cents

    @property
    def gas_left_cents(self) -> int:
        return self.gas_planned_cents - self.gas_actual_cents

    @property
    def variable_variance_cents(self) -> int:
        var = self.groceries_left_cents + self.gas_left_cents
        if self.dining_planned_cents > 0:
            var += self.dining_left_cents
        return var

    @property
    def total_personal_cents(self) -> int:
        """Planned personal spending envelope."""
        val = (
            self.income_cents
            - self.savings_target_cents
            - self.fixed_bills_cents
            - self.pge_cents
            - self.groceries_planned_cents
            - self.dining_planned_cents
            - self.gas_planned_cents
        )
        return max(0, val)

    @property
    def remaining_cushion_cents(self) -> int:
        """Dynamic remaining cushion."""
        effective_groc = self.groceries_actual_cents if self.groceries_actual_cents > 0 else self.groceries_planned_cents
        effective_gas = self.gas_actual_cents if self.gas_actual_cents > 0 else self.gas_planned_cents

        cushion = (self.groceries_planned_cents - effective_groc) + (self.gas_planned_cents - effective_gas)
        if self.dining_planned_cents > 0:
            effective_dining = self.dining_actual_cents if self.dining_actual_cents > 0 else self.dining_planned_cents
            cushion += (self.dining_planned_cents - effective_dining)
        return cushion

    def to_dict(self) -> Dict[str, Any]:
        return {
            "month": self.month,
            "status": self.status,
            "income_cents": self.income_cents,
            "savings_target_cents": self.savings_target_cents,
            "fixed_bills_cents": self.fixed_bills_cents,
            "pge_cents": self.pge_cents,
            "all_bills_cents": self.all_bills_cents,
            "groceries_planned_cents": self.groceries_planned_cents,
            "groceries_actual_cents": self.groceries_actual_cents,
            "groceries_left_cents": self.groceries_left_cents,
            "dining_planned_cents": self.dining_planned_cents,
            "dining_actual_cents": self.dining_actual_cents,
            "dining_left_cents": self.dining_left_cents,
            "gas_planned_cents": self.gas_planned_cents,
            "gas_actual_cents": self.gas_actual_cents,
            "gas_left_cents": self.gas_left_cents,
            "variable_variance_cents": self.variable_variance_cents,
            "total_personal_cents": self.total_personal_cents,
            "remaining_cushion_cents": self.remaining_cushion_cents,
            "notes": self.notes,
            "updated_at": self.updated_at,
        }


@dataclass
class GoalProgress:
    """Monthly goal tracker matching user's Notion mini progress tracker (Screenshot 4)."""
    name: str  # e.g. "💰 Savings" or "🏠 Kahram Payment"
    target_cents: int
    saved_cents: int
    status: str = "open"

    @property
    def remaining_cents(self) -> int:
        return max(0, self.target_cents - self.saved_cents)

    @property
    def progress_pct(self) -> float:
        if self.target_cents <= 0:
            return 100.0 if self.saved_cents > 0 else 0.0
        return min(100.0, round((self.saved_cents / self.target_cents) * 100.0, 1))

    @property
    def progress_bar(self) -> str:
        pct = self.progress_pct
        filled = int(round(pct / 10.0))
        empty = 10 - filled
        return f"[{'█' * filled}{'░' * empty}] {pct:.1f}%"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target_cents": self.target_cents,
            "saved_cents": self.saved_cents,
            "remaining_cents": self.remaining_cents,
            "progress_pct": self.progress_pct,
            "progress_bar": self.progress_bar,
            "status": self.status,
        }


@dataclass
class PaycheckPlan:
    """Bi-weekly or semi-monthly paycheck allocation rule."""
    paycheck_amount_cents: int
    savings_per_paycheck_cents: int
    frequency: str = "biweekly"  # 'biweekly' (every 14 days) or 'semimonthly' (1st & 15th)
    anchor_date: str = "2026-10-15"  # Known paycheck date YYYY-MM-DD
    updated_at: int = 0

    @property
    def net_checking_cents(self) -> int:
        """Net take-home remaining after immediate HYSA savings transfer."""
        return max(0, self.paycheck_amount_cents - self.savings_per_paycheck_cents)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paycheck_amount_cents": self.paycheck_amount_cents,
            "savings_per_paycheck_cents": self.savings_per_paycheck_cents,
            "net_checking_cents": self.net_checking_cents,
            "frequency": self.frequency,
            "anchor_date": self.anchor_date,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PaycheckPlan:
        return cls(
            paycheck_amount_cents=int(data["paycheck_amount_cents"]),
            savings_per_paycheck_cents=int(data["savings_per_paycheck_cents"]),
            frequency=str(data.get("frequency", "biweekly")),
            anchor_date=str(data.get("anchor_date", "2026-10-15")),
            updated_at=int(data.get("updated_at", 0)),
        )


@dataclass
class PaycheckWindowAllocation:
    """Calculated cash flow breakdown for a specific paycheck period."""
    paycheck_date: str          # YYYY-MM-DD
    next_paycheck_date: str     # YYYY-MM-DD
    days_in_cycle: int
    paycheck_amount_cents: int
    savings_transfer_cents: int
    net_checking_cents: int
    bills_due_cents: int
    bills: List[Dict[str, Any]]
    groceries_allocated_cents: int
    dining_allocated_cents: int
    gas_allocated_cents: int
    variable_needs_cents: int
    safe_to_spend_cents: int
    notes: str = ""
    other_bills: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def bills_due(self) -> List[Dict[str, Any]]:
        """Alias for bills list for compatibility."""
        return self.bills

    def to_dict(self) -> Dict[str, Any]:
        return {
            "paycheck_date": self.paycheck_date,
            "next_paycheck_date": self.next_paycheck_date,
            "days_in_cycle": self.days_in_cycle,
            "paycheck_amount_cents": self.paycheck_amount_cents,
            "savings_transfer_cents": self.savings_transfer_cents,
            "net_checking_cents": self.net_checking_cents,
            "bills_due_cents": self.bills_due_cents,
            "bills": self.bills,
            "other_bills": self.other_bills,
            "groceries_allocated_cents": self.groceries_allocated_cents,
            "dining_allocated_cents": self.dining_allocated_cents,
            "gas_allocated_cents": self.gas_allocated_cents,
            "variable_needs_cents": self.variable_needs_cents,
            "safe_to_spend_cents": self.safe_to_spend_cents,
            "notes": self.notes,
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
    Account("Expenses:Utilities:WiFi", AccountType.EXPENSE, description="Home Wi-Fi internet (fixed monthly)"),
    Account("Expenses:Utilities:Internet", AccountType.EXPENSE, description="Home internet (alias for Wi-Fi)"),
    Account("Expenses:Food:Groceries", AccountType.EXPENSE, description="Groceries and pantry (planned vs actual)"),
    Account("Expenses:Food:Dining", AccountType.EXPENSE, description="Eating out, restaurants, takeout (discretionary)"),
    Account("Expenses:Transport:Gas", AccountType.EXPENSE, description="Car fuel / gasoline (planned vs actual)"),
    Account("Expenses:Personal:Discretionary", AccountType.EXPENSE, description="Total personal spending buffer"),
    Account("Expenses:Family:Kahram", AccountType.EXPENSE, description="Recurring monthly payment to Kahram"),
    Account("Expenses:Family:Brother", AccountType.EXPENSE, description="Recurring monthly payment to brother (alias for Kahram)"),
    Account("Expenses:Subscriptions:Gym", AccountType.EXPENSE, description="Gym membership subscription"),
    Account("Expenses:Subscriptions:AppleCloud", AccountType.EXPENSE, description="Apple iCloud subscription"),
    Account("Expenses:Subscriptions:Spotify", AccountType.EXPENSE, description="Spotify music subscription"),

    # Equity
    Account("Equity:OpeningBalances", AccountType.EQUITY, description="Double-entry reconciliation initial balance"),
]

# ==============================================================================
# Default Recurring Rules Tailored to User Setup
# ==============================================================================
DEFAULT_USER_RECURRING_RULES: List[RecurringRule] = [
    RecurringRule(
        id="rule_kahram",
        name="Kahram payment",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=1,
        base_amount_cents=10000,  # Baseline fixed bill
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Family:Kahram",
        is_autopay=False,
        is_active=True,
        notes="Recurring fixed payment to Kahram due on the 1st.",
    ),
    RecurringRule(
        id="rule_gym",
        name="Gym",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=1,
        base_amount_cents=4500,
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:Gym",
        is_autopay=True,
        is_active=True,
        notes="Monthly gym membership due on the 1st.",
    ),
    RecurringRule(
        id="rule_apple_cloud",
        name="Apple iCloud",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=11,
        base_amount_cents=299,
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:AppleCloud",
        is_autopay=True,
        is_active=True,
        notes="Apple iCloud storage subscription due on the 11th.",
    ),
    RecurringRule(
        id="rule_spotify",
        name="Spotify",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=25,
        base_amount_cents=1199,
        from_account="Liabilities:CreditCard:Visa",
        to_account="Expenses:Subscriptions:Spotify",
        is_autopay=True,
        is_active=True,
        notes="Spotify music subscription due on the 25th.",
    ),
    RecurringRule(
        id="rule_wifi",
        name="Wi-Fi",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=15,
        base_amount_cents=5000,
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Utilities:WiFi",
        is_autopay=True,
        is_active=True,
        notes="Fixed monthly Wi-Fi bill.",
    ),
    RecurringRule(
        id="rule_internet",
        name="Internet",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=5,
        base_amount_cents=5000,
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Utilities:Internet",
        is_autopay=True,
        is_active=True,
        notes="Fixed monthly internet bill.",
    ),
    RecurringRule(
        id="rule_pge",
        name="PG&E Electric & Gas",
        rule_type=RuleType.VARIABLE,
        frequency=Frequency.MONTHLY,
        due_day_of_month=15,
        base_amount_cents=9000,
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Utilities:PGE",
        is_autopay=True,
        lead_time_days=7,
        is_active=True,
        notes="Variable utility bill; engine computes rolling moving average + seasonal buffer.",
    ),
    RecurringRule(
        id="rule_rent",
        name="Apartment Rent",
        rule_type=RuleType.FIXED,
        frequency=Frequency.MONTHLY,
        due_day_of_month=1,
        base_amount_cents=0,
        from_account="Assets:Checking:WellsFargo",
        to_account="Expenses:Housing:Rent",
        is_autopay=False,
        is_active=False,  # Inactive placeholder
        notes="Currently paid by dad; user starts paying after this year ends.",
    ),
]
