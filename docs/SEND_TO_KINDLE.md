# Send to Kindle: one-time setup

`math2kindle` delivers books by e-mailing them to your Kindle's Send-to-Kindle
address through your own SMTP account (Gmail in the examples below). Set this
up once, and after that every conversion can be sent automatically.

## 1. Find your Kindle's e-mail address

Amazon → **Account & Lists → Manage Your Content and Devices → Preferences →
Personal Document Settings**. Each device/app is listed under
*Send-to-Kindle E-Mail Settings* with an address like `name_AbC123@kindle.com`.

- `@kindle.com` delivers over Wi-Fi (and cellular, on models that have it).
- `@free.kindle.com` (older accounts) delivers **over Wi-Fi only**. math2kindle
  accepts it but warns you.

## 2. Approve the sending address

On the same page, under **Approved Personal Document E-mail List**, click
*Add a new approved e-mail address* and add the address you'll send from
(e.g. `you@gmail.com`). Amazon drops mail from addresses that aren't on this
list, and it doesn't tell the sender. Up to 15 addresses can be approved.

## 3. Create a Gmail App Password

Gmail won't accept your normal password over SMTP.

1. Turn on **2-Step Verification**: <https://myaccount.google.com/security>.
2. Create an App Password: <https://myaccount.google.com/apppasswords>
   (name it "math2kindle"). Copy the 16-character code. You can't view it again.

Workspace accounts may need an admin to allow App Passwords.

## 4. Write the config

```sh
mkdir -p ~/.config/math2kindle
cp math2kindle/config_example.toml ~/.config/math2kindle/config.toml
$EDITOR ~/.config/math2kindle/config.toml   # set kindle_email and from_email
```

Lookup order: `--config PATH`, then `$MATH2KINDLE_CONFIG`, then
`~/.config/math2kindle/config.toml`. The config file **never** holds the
password. math2kindle stops with an error if the file contains a password key.

## 5. Store the password

```sh
python -m math2kindle.send set-password
```

This prompts for the App Password without echoing it and stores it in the OS
keyring (macOS Keychain, or Secret Service/KWallet on Linux) under service
`math2kindle` with your `smtp_user` as the username. It requires
`pip install keyring`.

You can also set it as an environment variable, which overrides the keyring:

```sh
export MATH2KINDLE_SMTP_PASSWORD='abcdefghijklmnop'
```

## 6. Check, then send

```sh
python -m math2kindle.send check                    # validates config + SMTP login; sends nothing
python -m math2kindle.send book.epub --dry-run      # writes book.eml next to the file, no network
python -m math2kindle.send book.epub                # sends it
```

The book usually appears on the Kindle within a few minutes. **Amazon may
e-mail the sending address asking you to verify a document** (most often
the first time a new sender is used). Click the link in that e-mail, or the
book won't be delivered. If a document fails, Amazon sends a
"problem with your document" e-mail to the same address.

## What Amazon accepts by e-mail (checked October 2026)

- **Formats:** EPUB, PDF, DOC/DOCX, TXT, RTF, HTML/HTM, JPEG, GIF, PNG, BMP.
  math2kindle sends `.epub` (the normal output) and also accepts `.pdf`,
  `.docx` and `.txt`.
- **Not accepted:** MOBI/AZW/AZW3. Amazon dropped MOBI from Send to Kindle in
  2022, and AZW3 was never accepted by e-mail. math2kindle refuses these up
  front. Send the EPUB and Amazon converts it to its own format.
- **Size:** at most **50 MB** in total per e-mail, and up to 25 attachments.
  math2kindle sends one attachment and checks the 50 MB limit before
  connecting. The browser upload at <https://www.amazon.com/sendtokindle>
  accepts files up to 200 MB.
- **Gmail's own limit is 25 MB per message after encoding**, which works out
  to about 18 MB of attachment. math2kindle checks this when `smtp_host` is
  Gmail. For larger books, use another SMTP provider or the browser upload.
- The subject line doesn't matter. Amazon takes the title from the EPUB
  metadata or the filename. math2kindle sets the subject to the book title.
  Non-ASCII filenames such as `Élie–Cartan ζ-functions.epub` are encoded
  using RFC 2231.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SMTP login was rejected` | Use an App Password (step 3), make sure 2-Step Verification is on, and check that `smtp_user` is the full address. |
| `Server refused sender` | With Gmail, `from_email` must be the account's own address or a verified "Send mail as" alias. |
| Sent OK but nothing arrives | Check that the sender is on the Approved list (step 2), look for Amazon's verification or problem e-mail, and make sure the Kindle is on Wi-Fi (required for `@free.kindle.com`). |
| `TLS error` | Port 587 needs `smtp_security = "starttls"`. Port 465 needs `"ssl"`. |
| `No SMTP password available` | Run `set-password` (needs `keyring`) or export `MATH2KINDLE_SMTP_PASSWORD`. |

Sources: Amazon help pages "Learn About Sending Documents to Your Kindle
Library" (supported types) and "Learn How to Use Your Send to Kindle Email
Address" (50 MB, 25 attachments, approved list).
