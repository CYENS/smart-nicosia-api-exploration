# Demo script: dangerous buildings

A 15-question walk through the dangerous-building registers, the DLS 3D city and safer
routing, in six acts. Every question below was run against the chatbot on 20 Oct 2026 (21:45
to 23:10, after dark). The expected numbers are what came back then. The Limassol, Larnaca
and Famagusta registers and the camera counts are live, so a later run can differ slightly.

**Nicosia's numbers will change.** The chatbot now reads Nicosia's live register
(ndlgo.org.cy/en/hazardousbldsmap/), which lists 806 buildings (743 dangerous, 63 needing
repair). The site blocks the app after a few requests, so until the app gets through on a
daily refresh it answers from its saved copy of 711, and the numbers below are from that copy.
Once it has the live register, expect 1,020 buildings in all (instead of 925) and new counts
in questions 1, 2, 9 and 13. The live list page shows what to expect for Nicosia: Nicosia
city 211 (178 dangerous, 33 needing repair), Strovolos 58, Palaichori Morphou 32, Kato
Moni 29, Kakopetria 24, Spilia 23; 336 in urban areas and 470 in rural communities. Check
which copy it has with question 1: it says whether Nicosia's register is live or saved.

## Before you start

- Open <http://localhost:8010>. That server runs the current code. The one on 8000 was started
  before the registers, `where_am_i` and safer routing were added, so it can't answer half of this.
- Press **New chat** between acts if the map gets crowded.
- Each answer takes 10 to 20 seconds. Narrate while the tool chips tick by: they show what it is
  reading.
- Every answer says a listing is "a case on a public register, not an engineering verdict". That
  is deliberate; it is a good line to repeat to the audience.

## Act 1: the big picture

**1. How many dangerous buildings are on the registers across Cyprus, and where are they? Show me on the map.**

- Map: every register building island-wide, red for dangerous, amber for needs repair.
- Expect: 925 buildings, 865 dangerous (article 15B) and 60 needing repair (15A). Nicosia 711,
  Limassol 163, Famagusta 31, Larnaca 20. Paphos publishes no register.
- Talking point: four district registers, four different formats (an HTML map, an ArcGIS
  service, a WordPress table, two HTML tables), joined into one layer and tied to land parcels.

**2. Which neighbourhoods of Nicosia have the most? Make a chart.**

- Chart: sorted bar of the top 15 communities, coloured by status. Map: a density heatmap.
- Expect: Nicosia city 199, Strovolos 51, then mountain villages: Palaichori Morphou 30,
  Kato Moni 29, Spilia and Kakopetria 23 each.
- Talking point: it is not only an urban problem. The Troodos and Morphou villages fill the list.

## Act 2: the most dangerous

**3. What's the most dangerous building in Nicosia? Take me there in 3D.**

- The hero shot. The map turns on the DLS 3D city, tilts, and flies to a red tower among
  blue buildings, with a callout.
- Expect: it first says no register grades risk, then uses the tallest dangerous building as a
  stated stand-in: **Stasinou 44, Strovolos (Agios Dimitrios), 44.7 m, 16 floors.**
- Talking point: the answer is honest about what "most dangerous" can mean with this data.

**4. Across all of Cyprus, which dangerous building has the most building that could fall?**

- Map: flies to Limassol in 3D.
- Expect: ranked by volume, the **Kanika Enaerios complex** on Archbishop Makarios III Avenue
  at 28th October, Limassol: 264,830 m³, 41.7 m, 15 floors.
- Talking point: it spots the data quirk by itself. The complex is on the register as many
  separate entries on one plot, so it says "one complex rather than eight separate giants".

**5. Plot floors against height for every dangerous building. Does the data hang together?**

- Chart: a scatter of floors against height, coloured by status, with a trend line.
- Expect: about 2.8 m per floor; 640 of the 925 have both values; old-town blocks sit off the
  trend because DLS models a whole block as one building.
- Show: hover a point and that building lights up on the map.

**6. Draw a sankey of every register building: from district, to status, to height band.**

- A custom visual (d3, drawn by the model in a sandbox): district on the left, status in the
  middle, height bands on the right.
- Show: hover a ribbon to highlight its path.

## Act 3: the old town, relating data

**7. Around Laiki Geitonia, how do the dangerous buildings compare with everything else standing there? Show it in 3D.**

- Map: every DLS building within about 250 m extruded by height, register buildings in red,
  plus a height-against-floors chart.
- Expect: 104 buildings, median 13.4 m, tallest 66.9 m. 32 register entries nearby; the tallest
  is NIC-297 on Konstantinou Palaiologou at 33.6 m and 12 floors.
- Talking point: inside the walls DLS models whole blocks, and the answer says which heights
  are estimates.

**8. Where in the old town do the most people walk past dangerous buildings? Use the camera counts from this week.**

- The cross-source question: live pedestrian counts from the city's cameras against the
  registers. Map: 3D columns of people per camera, register buildings as red points.
- Expect: the **Ledras Square** camera, about 113,700 people in 7 days, with 8 dangerous
  buildings within 100 m. The nearest, 6 m from the camera, is NIC-73 at Ledras and Onasagorou
  (8 floors, 21 m). Rigainis Street comes second.
- Talking point: two public data sets nobody has joined before, answering "where is the
  exposure".

## Act 4: you are here

Set your position first: **right-click the map on Eleftheria Square and press "I'm here".**
The status line under the map then says your position is shared with the assistant.

**9. Where am I, and are there any dangerous buildings near me?**

- Map: you as a blue dot, the register buildings around you in 3D.
- Expect: on Kostaki Pantelidi, by Laiki Geitonia. 35 register buildings within 300 m
  (30 dangerous, 5 needing repair). The nearest is NIC-409, about 20 m away.

**10. I want to walk from here to Chrysaliniotissa Church. Will I pass any dangerous buildings?**

- It plans the safer route on its own and compares it with the usual one. Map: two routes, the usual one in grey and the safer one in blue, with a red "!" on every
  register building either passes.
- Expect (after dark): the usual route passes 7 register buildings in 2.16 km and 26 min; the
  safer one passes 2 in 2.55 km and 31.5 min, about 5 minutes more. The 2 it can't avoid are
  within 40 m of the start or the church.
- After dark it also prefers lit streets, so by day the minutes will differ a little.
- Alternative destination: **Famagusta Gate**. The usual route passes 6 register buildings in
  1.46 km; the safer one passes 1 in 1.85 km, 5.3 minutes more.

## Act 5: beyond Nicosia

**11. Show Larnaca's dangerous-building declarations over time.**

- Chart: declarations by date, with the red "now" line. Map: the 20 buildings.
- Expect: 20 buildings declared between 30 April and 9 October 2026, in batches; 7 in early
  October, most of them in Skala. The newest is LAR-120, 11 floors and 31.8 m.
- Talking point: only Larnaca dates its declarations, and it says so.

**12. Πόσα επικίνδυνα κτίρια υπάρχουν στο Καϊμακλί; Δείξε τα στον χάρτη.**

- Answers in Greek, with the buildings in 3D.
- Expect: 20 buildings, 16 dangerous and 4 needing repair; the tallest NIC-76 on
  Arrenagogeiou 8, 11.2 m.

## Act 6: search like the register site

The filters NDLGO's own pages offer: urban areas against communities, a file number (ΑΕΟ), a
plot.

**13. In Nicosia's register, how many buildings are in urban areas and how many in rural communities? Chart it by status.**

- Chart: buildings by zone and status.
- Expect: 309 in urban areas (268 dangerous, 41 needing repair) and 402 in rural communities
  (388 and 14). Urban ones are taller: median 10.5 m against 6.6 m.
- Talking point: almost every rural case is "dangerous"; repairs are an urban story.

**14. Find register file 540/2024 and show it on the map.**

- Map: one red building in 3D, in Alambra.
- Expect: Grigori Avxentiou, plot 39/1102V01 parcel 9, one floor and about 4 m.

**15. Which register buildings are on plan 21/460512? Show them in 3D.**

- Map: nine red buildings in the old town, in Trypiotis parish.
- Expect: 9, all dangerous, including the two 12-floor, 33.6 m towers on Konstantinou
  Palaiologou and the 8-floor block at Ledras and Onasagorou.

## Avoid during the demo

- **Multi-stop tours "avoiding dangerous buildings" in the old town.** A tour of the Leventis
  Museum, Faneromeni Church and the Cyprus Museum can't do better than the usual route there
  (every detour runs past other register buildings), so it says so and keeps the usual route,
  which passes 10. That answer is honest, but it is not a showpiece.
- **"Which one is closest to me?" right after question 9.** It gets there, but makes two empty
  lookups first and prints a stray working note. Question 9 already names the nearest one.
