# Open points — `dc-battery-sim` parameter contract

## OP-1 — `battery_sim_spec.md`'s thermal equation has no time factor (2026-09-15)

**Raised by:** ArchDev, while fixing bug thermal-dt 1.1.
**Root cause layer:** `spec`
**File:** `dc-battery-sim/battery_sim_spec.md:94-98`

The upstream simulator spec states the thermal integration as:

```
HeatDiff = kt2×I_dc² + kt1×I_dc + kt0 - ke×(T_battery - T_ambient) + P_heater - P_chiller

Heat = Heat_prev + HeatDiff
```

No `× sample_time`, even though the charge equation fourteen lines above it
(`battery_sim_spec.md:81`) does carry one: `Q_act = Q_prev + I_dc × sample_time`. Every term
of `HeatDiff` is a power in watts and `Heat` is in joules, so the equation as written is
dimensionally wrong. It is the literal source of bug thermal-dt 1.1 — `main.py` implemented
the spec faithfully.

`dc-battery-sim/README.md` has been corrected (`HeatFlow × sample_time`, units annotated).
`battery_sim_spec.md` has **not** been touched, because it is the upstream requirements
document rather than an ArchDev artifact, and silently rewriting a customer-supplied spec is
not a code fix.

**Decision needed from Buddy / the user:** either amend `battery_sim_spec.md:96` to
`Heat = Heat_prev + HeatDiff × sample_time`, or mark that block as superseded by README's
"Thermal Model" section. Left as-is, the next person to implement from the spec reintroduces
the same 10× bug.

**Not blocking.** The code and README are correct today; this is a documentation trap, not a
defect in the running service.
