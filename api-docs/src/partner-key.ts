export const ACTIVE_KEY = "tatva_partner_token";
export const SAVED_KEYS = "tatva_partner_tokens";

export const asAuthorization = (key: string) => (/^token\s/i.test(key) ? key : `token ${key}`);
