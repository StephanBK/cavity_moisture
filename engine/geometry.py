"""
Window and cavity geometry for the INOVUES SWR retrofit.

WHAT THIS IS FOR
----------------
The moisture engine works per square metre of glazing. That is the right
internal unit - but it is not how anyone specifies a window, and "320 grams
per square metre per year" is not a number a building operator can act on.

This module carries the real dimensions so the tool can:

  1. Take offset (cavity depth) as a design parameter, since that is the one
     geometric dimension that changes the per-m2 answer at all.
  2. Report per-window totals - litres of condensate on THIS window over a
     winter - alongside the normalised figures.
  3. Supply area and perimeter to a future vent-sizing model, where window
     size stops cancelling out.

THE CANCELLATION RESULT
-----------------------
At a FIXED air change rate, window width and height do not affect the
per-square-metre answer at all. Per m2 of glazing:

    cavity air mass   = A.d.rho / A = d.rho
    moisture supplied = ACH.A.d.rho.W / A = ACH.d.rho.W

Both depend on offset d alone; A has cancelled. So a 1 m2 window and a 6 m2
window with the same offset and the same ACH behave identically per unit area
- they differ only in TOTAL water, which scales with area.

Size stops cancelling once ACH is derived from vent hardware rather than
assumed, because vents live on the perimeter while volume scales with area:

    ACH  proportional to  P / (A.d)

See ``vent_geometry_ratio``. That model is deferred to a later chunk.

Doc ID: ANLY-002 R1.0, Chunk 3c
"""

from __future__ import annotations

from dataclasses import dataclass

#: Metres per inch, exact by definition.
M_PER_INCH = 0.0254

#: Density of liquid water, kg/m3. Used to report condensate in litres.
RHO_WATER = 1000.0


@dataclass(frozen=True)
class CavityGeometry:
    """Dimensions of one retrofitted window and its cavity.

    Parameters
    ----------
    width_m, height_m
        Glazing dimensions (sight line to sight line), metres.
    offset_m
        Cavity depth - the standoff between the existing pane and the new
        interior unit, metres. THE geometric design lever: it sets the cavity
        air mass per unit area, so a deeper cavity is a larger moisture
        reservoir that buffers more and responds more slowly.
        15.3 mm for the 277 Park SWR-VIG stack (ANLY-002 S2.3).
    label
        Free-text identifier for reporting.
    """

    width_m: float
    height_m: float
    offset_m: float = 0.0153
    label: str = ""

    def __post_init__(self) -> None:
        for name in ("width_m", "height_m", "offset_m"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.offset_m > 0.5:
            raise ValueError(
                f"offset_m = {self.offset_m} m is over half a metre. This is a "
                "glazing cavity, not a room - check you passed metres, not "
                "millimetres or inches."
            )

    # -- Constructors -------------------------------------------------------

    @classmethod
    def from_inches(
        cls,
        width_in: float,
        height_in: float,
        offset_in: float,
        label: str = "",
    ) -> "CavityGeometry":
        """Build from imperial dimensions, which is how US windows are specified.

        Conversion happens here, once, at the boundary - the same rule the
        psychrometric module follows for temperature.
        """
        return cls(
            width_m=width_in * M_PER_INCH,
            height_m=height_in * M_PER_INCH,
            offset_m=offset_in * M_PER_INCH,
            label=label,
        )

    # -- Derived quantities -------------------------------------------------

    @property
    def glazing_area_m2(self) -> float:
        """Glazing area, m2. Total condensate scales with this."""
        return self.width_m * self.height_m

    @property
    def perimeter_m(self) -> float:
        """Cavity perimeter, m. Vent slots live here, so this drives ACH once
        ventilation is derived rather than assumed."""
        return 2.0 * (self.width_m + self.height_m)

    @property
    def cavity_volume_m3(self) -> float:
        """Cavity volume, m3. Area x offset."""
        return self.glazing_area_m2 * self.offset_m

    @property
    def cavity_volume_litres(self) -> float:
        return self.cavity_volume_m3 * 1000.0

    @property
    def aspect_ratio(self) -> float:
        """Height / width. Tall cavities drive stronger stack effect, since
        buoyancy pressure scales with cavity height times temperature
        difference."""
        return self.height_m / self.width_m

    @property
    def vent_geometry_ratio(self) -> float:
        """P / (A.d), units 1/m2.

        The shape factor that will set ACH once vents are modelled: vent area
        scales with perimeter, the volume that must be flushed scales with
        area times offset. Larger windows and deeper cavities both vent more
        slowly for the same slot design.

        For a square window of side L this reduces to 4 / (L.d).
        """
        return self.perimeter_m / (self.glazing_area_m2 * self.offset_m)

    # -- Reporting ----------------------------------------------------------

    @property
    def width_in(self) -> float:
        return self.width_m / M_PER_INCH

    @property
    def height_in(self) -> float:
        return self.height_m / M_PER_INCH

    @property
    def offset_in(self) -> float:
        return self.offset_m / M_PER_INCH

    def per_window(self, value_per_m2: float) -> float:
        """Scale a per-m2 quantity to this whole window."""
        return value_per_m2 * self.glazing_area_m2

    def litres(self, kg_per_m2: float) -> float:
        """Convert a per-m2 water mass to litres on this whole window.

        1 kg of liquid water is 1 litre to within 0.3% over the temperature
        range that matters here, but the conversion is done explicitly rather
        than assumed so the constant is visible and auditable.
        """
        return self.per_window(kg_per_m2) / RHO_WATER * 1000.0

    def describe(self) -> dict:
        """Flat dictionary for the API response and the Excel summary page."""
        return {
            "label": self.label,
            "width_m": round(self.width_m, 4),
            "height_m": round(self.height_m, 4),
            "offset_m": round(self.offset_m, 5),
            "width_in": round(self.width_in, 2),
            "height_in": round(self.height_in, 2),
            "offset_in": round(self.offset_in, 3),
            "glazing_area_m2": round(self.glazing_area_m2, 4),
            "glazing_area_ft2": round(self.glazing_area_m2 * 10.7639, 2),
            "perimeter_m": round(self.perimeter_m, 4),
            "cavity_volume_litres": round(self.cavity_volume_litres, 3),
            "aspect_ratio": round(self.aspect_ratio, 3),
            "vent_geometry_ratio": round(self.vent_geometry_ratio, 2),
        }


#: The 277 Park reference window. Dimensions are a PLACEHOLDER - the offset is
#: on record in ANLY-002 S2.3 (15.3 mm gap), the width and height are not.
#: Replace with the real curtain wall module dimensions.
REFERENCE_277_PARK = CavityGeometry(
    width_m=1.5,
    height_m=2.5,
    offset_m=0.0153,
    label="277 Park reference module (dimensions provisional)",
)
