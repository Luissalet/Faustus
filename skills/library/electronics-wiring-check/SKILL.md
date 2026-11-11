---
name: electronics-wiring-check
description: Check a small electronics build by examining wiring, electrical limits and firmware pin use together, with sensor tolerances, test bands and an enclosure specification. Use when a circuit and its firmware must be verified before parts are bought, wired or powered.
version: 1.0.0
category: testing
tags: [electronics, wiring, netlist, firmware, pins, sensors, power-budget, enclosure, electronica, cableado, sensores, prototipo]
apps: [shannon]
status: published
source: imported
---

## When to Use

A prototype with a microcontroller, sensors, regulators and loads exists as a netlist, a schematic export or a written wiring list, plus firmware that assigns pins. Use this procedure to find mistakes before power is applied. It does not replace a measurement on the real board, and it does not certify products for safety, mains or medical use.

## Procedure

1. Read the app's tool list. Collect the netlist (import it if it comes from a schematic tool), the bill of parts with datasheet limits and the firmware pin assignments. If a part has no datasheet value you can cite, mark the limit unknown rather than assuming.
2. Check nets one at a time: every pin is connected as intended, no output drives another output, power and ground are not shorted, floating inputs have pull resistors, and shared buses (I2C addresses, chip selects) do not collide.
3. Check pin capabilities against use: a pin used for analog input actually has an analog converter, an interrupt pin supports interrupts, boot-strapping pins are not held at levels that block start-up, and the board's special pins are not used as ordinary outputs.
4. Check voltage and current limits in both directions. A 5 V output into a 3.3 V input is the classic fault; check absolute maximum ratings, not just typical levels, and add level shifting or dividers where needed. Compute the current that each pin sources and the total taken from the regulator.
5. Build the power budget: for each load the typical and worst-case current, summed per rail, compared with the regulator limit with margin. Check regulator dropout, input range and heat dissipation as (input minus output) times current.
6. Compare firmware with wiring: every pin the code uses exists in the wiring and vice versa, directions and pull settings agree, and constants for resolution and reference voltage match the hardware. A mismatch here compiles and fails silently.
7. Treat sensors as measurements with tolerances: combine accuracy, resolution and reference error into a worst-case band, and decide whether the band is acceptable. Where firmware uses lookup tables, test the table at its ends and a few middle points. Write physical test bands: what the instrument should read at which condition, and what deviation means a fault.
8. If the build goes in a case, write the enclosure specification: connector positions, cut-outs, ventilation for the dissipated power, mounting holes and clearances from the actual part dimensions.

## Pitfalls

- Trusting a typical datasheet figure where a limit applies.
- Checking wiring and firmware separately.
- Ignoring the start-up surge of motors and radios.
- Supply tolerances forgotten in the sensor error.
- Treating a rule check that passes as proof; it covers only the rules modelled.
- Case dimensions taken from drawings rather than the real parts.

## Verification

- Each finding cites a net, pin or part and the limit it breaks.
- The power budget shows margin per rail.
- Firmware pin list and netlist agree with no orphan on either side.
- Sensor error bands and test bands are written as numbers with units.
