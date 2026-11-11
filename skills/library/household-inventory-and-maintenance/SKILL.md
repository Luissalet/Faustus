---
name: household-inventory-and-maintenance
description: Record household objects and appliances with where they are, purchase and warranty details, and set maintenance tasks with their basis. Use when someone wants to know what they own and where it is, or what upkeep their home needs and when.
version: 1.0.0
category: planning
tags: [home, inventory, appliances, warranty, maintenance, reminders, packing, locations, hogar, inventario, garantia, mantenimiento]
apps: [homehoard]
status: published
source: imported
---

## When to Use

The person wants the house on record: where the drill is, which boiler they have, when the warranty ends, what must be serviced this year. This procedure covers adding items, recording appliance details, moving things, and creating maintenance tasks. It does not replace a technician's judgement on safety-critical equipment, and it does not store the purchase papers themselves; those live in the paperwork app and are linked.

## Procedure

1. Read the app's tool list. Decide the location scheme first, as a short hierarchy the person will recognise (home, room, furniture, container). Reuse existing places rather than creating near-duplicates such as "kitchen" and "Kitchen cupboard 1".
2. Add items with a name that a person would search for and a precise place. For things bought recently, use the purchase route (`item_add_from_purchase`) so date, price and shop are captured at once. For items with no history, leave purchase fields empty instead of guessing.
3. For appliances build the full card: brand, model, serial or model number from the label, purchase date, warranty end, spare parts, and a link to the receipt and manual held elsewhere. Photograph the rating plate; model numbers are exactly what you need in two years and nobody remembers them.
4. When something moves, record the move (`home_move_item`) at the moment it happens. If the person reports an item "somewhere in the garage", record that and refine later, noting the uncertainty.
5. Create maintenance tasks per item. Each needs: what is done, how often, when it was last done, who does it (self or technician), and the basis: manufacturer instruction, a legal requirement, or a general recommendation. Write the basis explicitly, because it decides how strictly to treat a missed date.
6. Set the first due date from the last real service, not from today, unless there is no record. Where the interval depends on use (filter changes), state the assumption.
7. For packing and moving, check a list against the inventory and note items not yet found or not in a kit; reusable kits (travel, tools) save repeating work.
8. Summarise what was added, what is overdue, and the facts still missing, such as models without a number.

## Pitfalls

- Locations that differ by one word and make search unreliable.
- Warranty end dates calculated from the delivery date when the terms run from purchase, or the reverse.
- Maintenance tasks without a basis, which later cannot be defended or dropped.
- Deleting a task to clear an alert; deleting needs an explicit confirmation for a reason.
- Treating a legal inspection and a cleaning reminder with the same urgency.
- Recording values read from memory instead of the label.

## Verification

- Every new item has a place that exists in the hierarchy.
- Every appliance has a model number or a note saying it could not be read.
- Every maintenance task has frequency, last-done date, responsible party and basis.
- Moves are recorded and a search by name returns the new place.
