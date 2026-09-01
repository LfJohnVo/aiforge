# Shared helpers for the peak.* packages.
#
# Classification and autonomy are ordered scales, and Rego has no enums, so both are
# mapped to integers here once. Comparing the strings would sort "C10" before "C2" the
# day someone adds a level, and would silently accept a typo as "not greater than".
package peak.common

# Data classification C0-C4.
rank := {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "C4": 4}

# Autonomy A0-A4.
autonomy_rank := {"A0": 0, "A1": 1, "A2": 2, "A3": 3, "A4": 4}

# An unknown value must not read as 0. Every lookup goes through these, which fail to the
# strictest end of the scale rather than the most convenient one.
level(value) := rank[value]

level(value) := 4 if {
	not rank[value]
}

autonomy(value) := autonomy_rank[value]

autonomy(value) := 4 if {
	not autonomy_rank[value]
}

# The highest classification an external, contractually governed model may ever see.
max_external := 2

# C3 and above never leave locally controlled infrastructure.
sovereign_required(classification) if {
	level(classification) >= 3
}

# A2 and above pause for a human.
needs_human(autonomy_level) if {
	autonomy(autonomy_level) >= 2
}

# A request has to name a tenant. Written as a positive helper and negated at the call
# site so that BOTH an absent field and an empty string deny: `input.tenant_id == ""` alone
# is undefined when the field is missing, and an undefined denial does not fire -- which is
# how an empty input document slips past every check in a package.
has_tenant if {
	input.tenant_id != ""
}

# The requester holds at least one of the groups an ACL names.
in_any_group(groups, required) if {
	some group in groups
	group in required
}
