"""Departments of the synthetic org: headcount shares, reporting lines and workforce mix."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Department:
    name: str
    code: str
    """Short slug used in role IDs (``role_fin_ops``)."""
    cost_center_base: int
    weight: float
    """Share of headcount in midmarket and enterprise orgs (percent)."""
    startup_weight: float
    """Share of headcount in a startup; 0 means the department doesn't exist there."""
    parent: str | None
    """Department whose head this department's head reports to (None: the CEO)."""
    contractor_affinity: float
    intern_affinity: float
    vendor_affinity: float
    """Relative likelihood that an individual contributor here is a non-employee. Contractors
    cluster in Engineering and IT, outsourced vendors in Support and Operations."""


DEPARTMENTS: tuple[Department, ...] = (
    Department("Executive", "exec", 1000, 0.5, 2.0, None, 0.0, 0.0, 0.0),
    Department("Finance", "fin", 1100, 3.5, 5.0, None, 0.6, 1.0, 0.1),
    Department("Accounting", "acct", 1200, 3.5, 0.0, "Finance", 0.6, 1.0, 0.1),
    Department("Procurement", "proc", 1300, 1.5, 0.0, "Finance", 0.6, 0.6, 0.1),
    Department("HR", "hr", 1400, 3.5, 4.0, None, 0.5, 0.8, 0.2),
    Department("Legal", "legal", 1500, 1.5, 0.0, None, 0.4, 0.5, 0.1),
    Department("IT", "it", 2100, 5.0, 4.0, None, 1.8, 0.8, 2.0),
    Department("Security", "sec", 2200, 2.5, 0.0, None, 0.8, 0.6, 0.3),
    Department("Engineering", "eng", 3100, 22.0, 40.0, None, 2.0, 1.5, 0.5),
    Department("Product", "prod", 3200, 5.0, 10.0, None, 0.8, 1.2, 0.2),
    Department("Data", "data", 3300, 5.0, 0.0, "Engineering", 1.5, 1.5, 0.3),
    Department("Sales", "sales", 4100, 14.0, 15.0, None, 0.6, 0.8, 0.3),
    Department("Marketing", "mkt", 4200, 6.0, 8.0, None, 1.0, 1.5, 0.3),
    Department("Customer Support", "sup", 4300, 13.0, 12.0, None, 1.2, 0.5, 4.0),
    Department("Operations", "ops", 5100, 13.5, 0.0, None, 1.5, 0.6, 2.5),
)

TRANSITIONS_INTO: dict[str, tuple[str, ...]] = {
    "Executive": (),
    "Finance": ("Accounting", "Procurement", "Data", "Sales"),
    "Accounting": ("Finance", "Procurement", "Operations"),
    "Procurement": ("Operations", "Finance", "Accounting"),
    "HR": ("Legal", "Operations", "Finance"),
    "Legal": ("HR", "Finance"),
    "IT": ("Security", "Engineering", "Customer Support"),
    "Security": ("IT", "Engineering"),
    "Engineering": ("Data", "IT", "Product", "Security"),
    "Product": ("Engineering", "Marketing", "Data", "Customer Support"),
    "Data": ("Engineering", "Finance", "Product"),
    "Sales": ("Marketing", "Customer Support", "Product"),
    "Marketing": ("Sales", "Product", "Data"),
    "Customer Support": ("Sales", "Operations", "IT"),
    "Operations": ("Procurement", "Customer Support", "Sales"),
}
"""Plausible internal moves: for a mover now in department D, the departments they may have
come from. Moves between adjacent functions are what create old-birthright leftovers (and,
for Procurement -> Operations, a real SoD overlap) worth testing."""
