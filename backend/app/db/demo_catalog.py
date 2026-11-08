"""Demo catalogue rows for the seed.

Chosen so the substitution feature is visible immediately: several
compositions carry three or more competing brands at *different prices*,
which is exactly the case a counter operator uses the alternatives panel for.

Pack measure varies by dosage form on purpose — strips of tablets, bottles in
ml, tubes in grams, vials — to exercise the rules in
``services.catalog.resolve_pack``.

Tuple order:
    (brand, manufacturer, composition, therapeutic class, form,
     pack_size, pack_unit, schedule, hsn, gst %, mrp_per_pack)
"""

DEMO_MEDICINES = [
    # --- Paracetamol 500 MG: 4 brands at 4 prices ------------------------
    ("Crocin 500", "GlaxoSmithKline", "Paracetamol 500mg", "Analgesic / Antipyretic",
     "TABLET", "15", "TABLET", "OTC", "30049099", "12", "30.24"),
    ("Calpol 500", "GlaxoSmithKline", "Paracetamol 500mg", "Analgesic / Antipyretic",
     "TABLET", "15", "TABLET", "OTC", "30049099", "12", "27.50"),
    ("Pacimol 500", "Ipca Laboratories", "Paracetamol 500mg", "Analgesic / Antipyretic",
     "TABLET", "10", "TABLET", "OTC", "30049099", "12", "14.00"),
    ("Paracip 500", "Cipla", "Paracetamol 500mg", "Analgesic / Antipyretic",
     "TABLET", "10", "TABLET", "OTC", "30049099", "12", "12.60"),
    ("Dolo 650", "Micro Labs", "Paracetamol 650mg", "Analgesic / Antipyretic",
     "TABLET", "15", "TABLET", "OTC", "30049099", "12", "34.50"),

    # --- Amoxicillin + Clavulanic acid: 3 brands -------------------------
    ("Augmentin 625", "GlaxoSmithKline", "Amoxicillin 500mg + Clavulanic Acid 125mg",
     "Antibiotic", "TABLET", "10", "TABLET", "SCHEDULE_H", "30041020", "12", "223.50"),
    ("Clavam 625", "Alkem Laboratories", "Amoxicillin 500mg + Clavulanic Acid 125mg",
     "Antibiotic", "TABLET", "10", "TABLET", "SCHEDULE_H", "30041020", "12", "198.00"),
    ("Moxikind-CV 625", "Mankind Pharma", "Amoxicillin 500mg + Clavulanic Acid 125mg",
     "Antibiotic", "TABLET", "10", "TABLET", "SCHEDULE_H", "30041020", "12", "172.40"),

    # --- Pantoprazole 40 MG: 3 brands ------------------------------------
    ("Pantop 40", "Aristo Pharmaceuticals", "Pantoprazole 40mg", "Proton pump inhibitor",
     "TABLET", "15", "TABLET", "SCHEDULE_H", "30049069", "12", "135.00"),
    ("Pan 40", "Alkem Laboratories", "Pantoprazole 40mg", "Proton pump inhibitor",
     "TABLET", "15", "TABLET", "SCHEDULE_H", "30049069", "12", "159.00"),
    ("Pantocid 40", "Sun Pharmaceutical", "Pantoprazole 40mg", "Proton pump inhibitor",
     "TABLET", "15", "TABLET", "SCHEDULE_H", "30049069", "12", "148.50"),

    # --- Metformin 500 MG: 3 brands, 3 strip sizes -----------------------
    ("Glycomet 500", "USV", "Metformin 500mg", "Antidiabetic",
     "TABLET", "20", "TABLET", "SCHEDULE_H", "30049099", "12", "36.80"),
    ("Gluconorm 500", "Lupin", "Metformin 500mg", "Antidiabetic",
     "TABLET", "15", "TABLET", "SCHEDULE_H", "30049099", "12", "31.50"),
    ("Metsmall 500", "Cipla", "Metformin 500mg", "Antidiabetic",
     "TABLET", "10", "TABLET", "SCHEDULE_H", "30049099", "12", "18.00"),

    # --- Amlodipine 5 MG: 3 brands ---------------------------------------
    ("Amlokind 5", "Mankind Pharma", "Amlodipine 5mg", "Antihypertensive",
     "TABLET", "10", "TABLET", "SCHEDULE_H", "30049099", "12", "22.00"),
    ("Amlopres 5", "Cipla", "Amlodipine 5mg", "Antihypertensive",
     "TABLET", "10", "TABLET", "SCHEDULE_H", "30049099", "12", "48.50"),
    ("Stamlo 5", "Dr Reddys Laboratories", "Amlodipine 5mg", "Antihypertensive",
     "TABLET", "10", "TABLET", "SCHEDULE_H", "30049099", "12", "39.20"),

    # --- Cetirizine 10 MG: 2 brands --------------------------------------
    ("Cetzine 10", "GlaxoSmithKline", "Cetirizine 10mg", "Antihistamine",
     "TABLET", "10", "TABLET", "OTC", "30049099", "12", "39.00"),
    ("Alerid 10", "Cipla", "Cetirizine 10mg", "Antihistamine",
     "TABLET", "10", "TABLET", "OTC", "30049099", "12", "32.75"),

    # --- Azithromycin 500 MG: 2 brands, different strip sizes ------------
    ("Azithral 500", "Alembic Pharmaceuticals", "Azithromycin 500mg", "Antibiotic",
     "TABLET", "5", "TABLET", "SCHEDULE_H", "30042031", "12", "132.00"),
    ("Azee 500", "Cipla", "Azithromycin 500mg", "Antibiotic",
     "TABLET", "3", "TABLET", "SCHEDULE_H", "30042031", "12", "82.50"),

    # --- Combination analgesic -------------------------------------------
    ("Combiflam", "Sanofi India", "Ibuprofen 400mg + Paracetamol 325mg", "Analgesic",
     "TABLET", "20", "TABLET", "OTC", "30049099", "12", "48.00"),
    ("Flexon", "Aristo Pharmaceuticals", "Ibuprofen 400mg + Paracetamol 325mg", "Analgesic",
     "TABLET", "20", "TABLET", "OTC", "30049099", "12", "42.60"),

    # --- Capsules ---------------------------------------------------------
    ("Vitamin D3 60K", "Cadila Healthcare", "Cholecalciferol 60000IU", "Supplement",
     "CAPSULE", "4", "CAPSULE", "OTC", "30045020", "12", "89.00"),
    ("Becosules", "Pfizer", "Vitamin B Complex 1mg + Vitamin C 75mg", "Supplement",
     "CAPSULE", "20", "CAPSULE", "OTC", "30045020", "12", "42.00"),

    # --- Syrups: measured in ml, one sellable unit per bottle -------------
    ("Benadryl Cough Syrup", "Johnson & Johnson",
     "Diphenhydramine 14mg + Ammonium Chloride 138mg", "Cough preparation",
     "SYRUP", "100", "ML", "OTC", "30049099", "12", "128.00"),
    ("Ascoril LS Syrup", "Glenmark Pharmaceuticals",
     "Ambroxol 30mg + Levosalbutamol 1mg + Guaifenesin 50mg", "Cough preparation",
     "SYRUP", "100", "ML", "SCHEDULE_H", "30049099", "12", "119.00"),
    ("Crocin Syrup 60ml", "GlaxoSmithKline", "Paracetamol 125mg", "Analgesic / Antipyretic",
     "SYRUP", "60", "ML", "OTC", "30049099", "12", "45.00"),

    # --- Ointments and gels: measured in grams ---------------------------
    ("Betadine Ointment", "Win-Medicare", "Povidone Iodine 5%", "Antiseptic",
     "OINTMENT", "20", "GM", "OTC", "30049099", "12", "98.00"),
    ("Volini Gel", "Sun Pharmaceutical", "Diclofenac Diethylamine 1.16%", "Topical analgesic",
     "OINTMENT", "30", "GM", "OTC", "30049099", "12", "145.00"),

    # --- Drops: measured in ml -------------------------------------------
    ("Otrivin Nasal Drops", "Novartis India", "Xylometazoline 0.1%", "Nasal decongestant",
     "DROPS", "10", "ML", "OTC", "30049099", "12", "72.00"),

    # --- Injections: vials and ml ----------------------------------------
    ("Monocef 1G Injection", "Aristo Pharmaceuticals", "Ceftriaxone 1g", "Antibiotic",
     "INJECTION", "1", "VIAL", "SCHEDULE_H1", "30042019", "12", "68.00"),
    ("Meropenem 1G Injection", "Cipla", "Meropenem 1g", "Antibiotic",
     "INJECTION", "1", "VIAL", "SCHEDULE_H1", "30042019", "12", "1250.00"),
    ("Insulin Human 40IU", "Novo Nordisk", "Human Insulin 40IU", "Antidiabetic",
     "INJECTION", "10", "ML", "SCHEDULE_H", "30043110", "5", "165.00"),

    # --- Powder: measured in grams ---------------------------------------
    ("ORS Powder", "FDC Limited",
     "Sodium Chloride 2.6g + Potassium Chloride 1.5g + Glucose 13.5g", "Rehydration",
     "POWDER", "21.8", "GM", "OTC", "30049099", "5", "22.00"),
]
