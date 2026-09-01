# The effective autonomy of an action, and whether a human has to see it first.
#
# Two different quantities share the letter A, and confusing them is the classic way an
# anonymous caller ends up executing an A1 action:
#
#   * **required** -- how much independence the action needs. Combines by MAXIMUM across
#     the tool's declaration, the profile and this policy. No layer can lower another's.
#   * **granted**  -- how much this requester may exercise without a human. Combines by
#     MINIMUM.
#
# This package answers about *required*, and returns the grant separately.
package peak.autonomy

import data.peak.common

default decision := {
	"allow": false,
	"reasons": ["default deny"],
	"obligations": [],
}

decision := {
	"allow": count(denials) == 0,
	"reasons": reasons_for(denials),
	"obligations": obligations_for(input),
	"autonomy": granted,
} if {
	granted := granted_level
}

reasons_for(denials) := [sprintf("autonomy granted up to %v", [granted_level])] if {
	count(denials) == 0
} else := [reason | some reason in denials]

# ------------------------------------------------------------------------ grant

# An unauthenticated caller gets A0: read only, no side effects. Not A1 -- "reversible
# and low impact" is still an effect, and there is nobody to attribute it to.
granted_level := "A0" if {
	not input.authenticated
} else := "A0" if {
	# Acting on restricted data is never autonomous, however trusted the requester.
	common.sovereign_required(input.classification)
} else := "A1"

# ---------------------------------------------------------------------- denials

# Without this, an input document missing every field produced *no* denials -- each rule
# body referenced an absent field and was therefore undefined -- and the package allowed
# it. Default deny only protects the case where `decision` itself is undefined, not the
# case where it evaluates over nothing.
denials contains "a request without a tenant cannot be scoped" if {
	not common.has_tenant
}

denials contains "the request does not declare an autonomy level" if {
	not declares_autonomy
}

declares_autonomy if {
	common.autonomy_rank[input.autonomy_declared]
}

denials contains sprintf(
	"the action needs %v but this requester is granted %v",
	[input.autonomy_declared, granted_level],
) if {
	common.autonomy(input.autonomy_declared) > common.autonomy(granted_level)
	not common.needs_human(input.autonomy_declared)
}

# ------------------------------------------------------------------ obligations

# An action above the grant is not denied outright: it is escalated. Denying it would
# make approval impossible, which is the opposite of what HITL is for.
obligations_for(request) := ["human_approval"] if {
	common.needs_human(request.autonomy_declared)
} else := []
