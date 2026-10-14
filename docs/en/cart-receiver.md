# Cart token and the receiver contract

**English** · [Русский](../ru/cart-receiver.md)

For whom: the author of a store-side module (Bitrix, OpenCart, PrestaShop, Magento, Shop-Script) that takes a cart link from shopmcp and fills the customer's cart. The same token is what the `form_post` trampoline carries in its URL.

Why a token at all: the server keeps no carts. The whole handoff lives in the link, so it must not be editable and must expire. The token is **not** a JWT: a module verifies it with HMAC-SHA256 and a constant-time comparison, without a library and without an algorithm to choose.

Contents: [Format](#format) · [Verifying](#verifying) · [Receiver behaviour](#receiver-behaviour) · [Test vectors](#test-vectors) · [What the signature does not give](#what-the-signature-does-not-give)

## Format

```
<b64url(payload_json)>.<b64url(HMAC-SHA256(secret, b64url(payload_json)))>
```

- `b64url` is base64url (RFC 4648 §5) **without** `=` padding.
- The MAC is computed over the ASCII text of the first part, not over the decoded JSON: the JSON is never re-serialized.
- The whole token is at most 1800 characters.

Payload:

```json
{"v":1,"kid":"1","s":"<store id>","iat":1760400000,"exp":1760486400,"jti":"<16 random bytes, b64url>",
 "l":[["<product_id>","<variant_id or null>","<quantity as a decimal string>",{"<option>":"<value>"}]]}
```

| Field | Meaning |
|---|---|
| `v` | Format version, `1` |
| `kid` | Key id: picks the secret, so a key can be rotated without breaking links in flight |
| `s` | Store id (`id` in `store.toml`): a token for another store is refused |
| `iat`, `exp` | Issued at / expires at, Unix seconds |
| `jti` | Unique token id: a repeated click on the same link must not add the lines twice |
| `l` | Lines: product id as the store's cart knows it, variant id or `null`, quantity (`"2"`, `"1.5"`), options (name → value; empty object if none) |

Ids match `^[A-Za-z0-9._:-]{1,128}$`; at most 20 options per line, values up to 200 characters. Prices are never in the token: the store prices the cart itself.

## Verifying

1. Length ≤ 1800, exactly one `.`, both parts non-empty.
2. Decode the first part, parse JSON, read `kid`, take the secret for it (unknown `kid` → refuse).
3. Compute HMAC-SHA256 with that secret over the first part **as received**, encode b64url without padding, compare with the second part in constant time (`hash_equals` in PHP, `hmac.compare_digest` in Python).
4. `v == 1`, `s` equals your store id, `exp` and `iat` are integers; refuse if `now > exp + 60` or `iat > now + 60` (60 s of clock skew).
5. Every line: ids match the pattern above, quantity is a positive finite decimal.

The secret is the value of the environment variable named in `cart.secret` (at least 32 bytes), configured identically on both sides.

## Receiver behaviour

Normative for every module:

- `GET <receiver path>?mcp_cart=<token>`.
- If the request has `Sec-Fetch-Dest` and it is not `document` → `403`.
- Verify the token as above.
- **Add** the lines to the session cart; never replace it.
- Remember `jti` in the session: a token seen before is not applied again, the customer is just redirected to the cart.
- A product that is unavailable or unknown is skipped, with a flash message.
- The result is a `302` to the cart page. An error is a `302` to the cart with a message — never a `500`, never a page that shows the token.

## Test vectors

Secret (`kid` = `"1"`): `test-secret-0123456789-abcdefghij` (33 bytes, ASCII). Store id: `demo`.

Payload (exactly these bytes, UTF-8):

```json
{"v":1,"kid":"1","s":"demo","iat":1760400000,"exp":1760486400,"jti":"AAECAwQFBgcICQoLDA0ODw","l":[["1001","2002","2",{}],["77",null,"1.5",{"Размер":"M"}]]}
```

Token:

```
eyJ2IjoxLCJraWQiOiIxIiwicyI6ImRlbW8iLCJpYXQiOjE3NjA0MDAwMDAsImV4cCI6MTc2MDQ4NjQwMCwianRpIjoiQUFFQ0F3UUZCZ2NJQ1FvTERBME9EdyIsImwiOltbIjEwMDEiLCIyMDAyIiwiMiIse31dLFsiNzciLG51bGwsIjEuNSIseyLQoNCw0LfQvNC10YAiOiJNIn1dXX0.QJCt2zh7AIss7pR9RmjTB5Nbbx2TGsdvO0NsNlwDR5Q
```

| Case | Input | Expected |
|---|---|---|
| Valid | the token, store `demo`, now = `1760400100` | accepted; lines `1001`/`2002` × 2 and `77` × 1.5 with option `Размер` = `M` |
| Expired | the token, now = `1760486461` (`exp` + 61) | refused: expired (`1760486460` is still accepted) |
| Another store | the token, store `other` | refused: another store |
| Broken signature | the token with its last character changed | refused: bad signature |
| Unknown key | the token, only `kid` `"2"` configured | refused: unknown key id |

Check the MAC without any code of ours:

```sh
printf %s 'eyJ2Ijox…XX0' | openssl dgst -sha256 -hmac 'test-secret-0123456789-abcdefghij' -binary \
  | openssl base64 -A | tr '+/' '-_' | tr -d '='
# QJCt2zh7AIss7pR9RmjTB5Nbbx2TGsdvO0NsNlwDR5Q   (first part of the token in place of 'eyJ2Ijox…XX0')
```

The same vectors are in `tests/unit/test_cart_token.py`.

## What the signature does not give

The signature proves the list came from this server, unchanged and in time, for this store. It does **not** stop anyone from sending a valid cart link to someone else: the cart tool is public, so anyone can get a signed link. Against a page that silently fills a visitor's cart, the receiver relies on `Sec-Fetch-Dest: document` (a top-level navigation, not an image or a frame), on the limits on lines and quantities, on accepting only active products, and on `jti`.
