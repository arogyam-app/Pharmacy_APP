// Mirrors backend/app/models/enums.py — keep the two in sync.

export const UserRole = {
  ADMIN: 'ADMIN',
  SALES_EXEC: 'SALES_EXEC',
}

export const SaleStatus = {
  DRAFT: 'DRAFT',
  COMPLETED: 'COMPLETED',
  VOID_REQUESTED: 'VOID_REQUESTED',
  VOIDED: 'VOIDED',
  PARTIALLY_RETURNED: 'PARTIALLY_RETURNED',
  RETURNED: 'RETURNED',
}

export const PaymentMode = {
  CASH: 'CASH',
  CARD: 'CARD',
  UPI: 'UPI',
  NET_BANKING: 'NET_BANKING',
  CREDIT_NOTE: 'CREDIT_NOTE',
  OTHER: 'OTHER',
}

/**
 * A pharmacy counter also sells soap, nappies and a thermometer. Those share
 * a batch, an MRP, a GST rate and a place on the same bill with medicines,
 * and share none of the pharmacological attributes — so they are one
 * catalogue with two kinds of row. Mirrors ItemType in
 * backend/app/models/enums.py.
 */
export const ItemType = {
  MEDICINE: 'MEDICINE',
  GENERAL: 'GENERAL',
}

export const ITEM_TYPE_LABEL = {
  MEDICINE: 'Medicine',
  GENERAL: 'General',
}

export const DosageForm = {
  TABLET: 'TABLET',
  CAPSULE: 'CAPSULE',
  SYRUP: 'SYRUP',
  INJECTION: 'INJECTION',
  OINTMENT: 'OINTMENT',
  DROPS: 'DROPS',
  POWDER: 'POWDER',
  OTHER: 'OTHER',
  // Not a dosage form — what a general item carries so that pack resolution,
  // which keys off this field, has something to work with. Never offered as
  // a dosage form in the UI.
  GENERAL: 'GENERAL',
}

export const PackUnit = {
  TABLET: 'TABLET',
  CAPSULE: 'CAPSULE',
  ML: 'ML',
  GM: 'GM',
  IU: 'IU',
  VIAL: 'VIAL',
  UNIT: 'UNIT',
  // One countable article — a soap, a razor, a nappy.
  PIECE: 'PIECE',
}

// Mirrors PACK_UNITS_BY_FORM in backend/app/models/enums.py.
// Only tablets and capsules come in countable strips; everything else is a
// single container measured by volume or weight.
export const PACK_UNITS_BY_FORM = {
  TABLET: ['TABLET'],
  CAPSULE: ['CAPSULE'],
  SYRUP: ['ML'],
  DROPS: ['ML'],
  INJECTION: ['ML', 'VIAL', 'IU'],
  OINTMENT: ['GM', 'ML'],
  POWDER: ['GM', 'ML'],
  OTHER: ['UNIT', 'ML', 'GM'],
  GENERAL: ['PIECE', 'ML', 'GM', 'UNIT'],
}

export const COUNTABLE_FORMS = ['TABLET', 'CAPSULE', 'GENERAL']

// The finer test, and the one that decides units-per-pack: a general item can
// be a box of twelve soaps (PIECE — twelve sellable units) or a 500 ml bottle
// of cleaner (ML — one). It is the unit, not the form, that says whether the
// pack holds countable articles.
export const COUNTABLE_UNITS = ['TABLET', 'CAPSULE', 'PIECE']

/**
 * How a pack states its shelf life. Mirrors ExpiryBasis in
 * backend/app/models/enums.py.
 */
export const ExpiryBasis = {
  EXPIRY_DATE: 'EXPIRY_DATE',
  BEST_BEFORE: 'BEST_BEFORE',
  NONE: 'NONE',
}

export const EXPIRY_BASIS_LABEL = {
  EXPIRY_DATE: 'Expiry date on the pack',
  BEST_BEFORE: 'Mfg date + best before',
  NONE: 'Does not expire',
}

/** Label for the pack-size field, which changes with the dosage form. */
export const PACK_SIZE_LABEL = {
  TABLET: 'Tablets per strip',
  CAPSULE: 'Capsules per strip',
  ML: 'Volume (ml)',
  GM: 'Net weight (g)',
  IU: 'Strength (IU)',
  VIAL: 'Vials per pack',
  UNIT: 'Units per pack',
  PIECE: 'Pieces per pack',
}

export const PACK_UNIT_LABEL = {
  TABLET: 'tablets',
  CAPSULE: 'capsules',
  ML: 'ml',
  GM: 'g',
  IU: 'IU',
  VIAL: 'vial(s)',
  UNIT: 'unit(s)',
  PIECE: 'piece(s)',
}

/** What one sellable unit is called, for price-per-unit captions. */
export const SELLABLE_UNIT_LABEL = {
  TABLET: 'tablet',
  CAPSULE: 'capsule',
  ML: 'bottle',
  GM: 'pack',
  IU: 'pack',
  VIAL: 'vial',
  UNIT: 'unit',
  PIECE: 'piece',
}

export const DEFAULT_PACK_SIZE = {
  TABLET: 10,
  CAPSULE: 10,
  SYRUP: 100,
  DROPS: 10,
  INJECTION: 1,
  OINTMENT: 20,
  POWDER: 10,
  OTHER: 1,
  GENERAL: 1,
}

export const ItemCondition = {
  SALEABLE: 'SALEABLE',
  DAMAGED: 'DAMAGED',
  EXPIRED: 'EXPIRED',
}

export const SALE_STATUS_LABEL = {
  DRAFT: 'Draft',
  COMPLETED: 'Completed',
  VOID_REQUESTED: 'Void requested',
  VOIDED: 'Voided',
  PARTIALLY_RETURNED: 'Partly returned',
  RETURNED: 'Returned',
}

export const PAYMENT_MODE_LABEL = {
  CASH: 'Cash',
  CARD: 'Card',
  UPI: 'UPI',
  NET_BANKING: 'Net banking',
  CREDIT_NOTE: 'Credit note',
  OTHER: 'Other',
}
