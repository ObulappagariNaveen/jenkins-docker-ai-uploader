# Jenkins Log AI Explainer - Current Workflow

This document explains what we changed, why we changed it, and how the website works now.

## Goal

The website helps colleagues understand Jenkins job input failures in simple plain English.

Users upload:

1. The failed Jenkins `consoleText` log.
2. The CSV input file that was uploaded to Jenkins.
3. The Jenkins job name from a dropdown.

The website checks the uploaded CSV against the stored correct CSV format for that selected Jenkins job. If the CSV has format or data problems, the website gives the exact issue and fix directly in simple text.

## Current Website

Open the website on your Mac:

```text
http://localhost:8081
```

For colleagues on the same network, use your current Mac IP address:

```text
http://YOUR-MAC-IP:8081
```

Example:

```text
http://192.168.31.2:8081
```

Your Mac must stay on, Docker must be running, and Ollama must be running.

## Current Docker Setup

Recommended compose file:

```text
docker-compose.local-ollama.yml
```

Start the website:

```bash
cd /Users/obulappagari.naveen/Documents/Codex/2026-05-10/files-mentioned-by-the-user-jenkins/jenkins-docker-ai-uploader
docker compose -f docker-compose.local-ollama.yml up -d --no-build
```

If the image is missing:

```bash
docker compose -f docker-compose.local-ollama.yml up -d --build
```

Check containers:

```bash
docker compose -f docker-compose.local-ollama.yml ps
```

## What Runs Where

Docker:

```text
Website
Uploaded Jenkins logs
Uploaded Jenkins input CSVs
Generated AI explanations
Archived logs
Stored reference formats mounted read-only
```

Mac:

```text
Ollama app
jenkins-failure-explainer model
```

The web container calls Ollama using:

```text
http://host.docker.internal:11434
```

## Docker Storage

Inside Docker, files are stored here:

```text
/data/input     - uploaded Jenkins console logs
/data/support   - uploaded Jenkins input CSV files
/data/output    - generated AI explanation files
/data/archive   - archived raw Jenkins logs
```

Check stored files:

```bash
docker exec jenkins-ai-web-local-ollama find /data -maxdepth 2 -type f -print
```

## Reference Formats

Correct Jenkins input formats are stored in this repo:

```text
reference_formats/
```

Each Jenkins job gets its own folder:

```text
reference_formats/<JENKINS_JOB_NAME>/correct_input.csv
```

Current stored job:

```text
reference_formats/User_role_access/correct_input.csv
reference_formats/fm_existing_partner_location_onboarding/correct_input.csv
```

The website dropdown is built from these folders. If a folder has `correct_input.csv`, it appears as a selectable Jenkins job.

## Current Job: User_role_access

Correct reference CSV:

```text
User_contact_number,Role
9005097898,FE
9005097898,ADMIN
9005097898,AM
```

The website infers these checks:

```text
Required exact columns:
- User_contact_number
- Role

User_contact_number:
- required
- digits only
- exactly 10 digits

Role:
- required
- must be one of ADMIN, AM, FE
```

It also checks:

```text
No extra spaces in headers
No leading or trailing spaces in values
Headers must be spelled exactly
Headers must be in the correct order
The input file must be .csv
```

## Current Job: fm_existing_partner_location_onboarding

Correct reference CSV:

```text
fmcode,fmsc,partner_id,contactNumber,branch_admin_name,email,clientLocationName,fmcodeaddress,loczipcode,pickupPincodes,city_id,isMigratedLocation
ZCC,S10S,135264,,MUKESHBHAI DHIRUBHAI,allinone.logistics15@gmail.com,ZCC,"38, Siddheshwar Camp, opp. Eagle party plot, Sarthana Jakatnaka, Surat-395013",395022,"395006, 395010, 395013, 394185",9,0
```

The website checks:

```text
Required exact columns:
- fmcode
- fmsc
- partner_id
- contactNumber
- branch_admin_name
- email
- clientLocationName
- fmcodeaddress
- loczipcode
- pickupPincodes
- city_id
- isMigratedLocation

Field and data rules:
- No extra spaces before or after headers.
- No extra spaces before or after values.
- fmcode and clientLocationName must be exactly same.
- fmcode and clientLocationName must be uppercase.
- partner_id must be integer only.
- city_id must be integer only.
- isMigratedLocation must always be 0.
- Special characters are blocked in normal fields.
- Special characters are allowed in email and fmcodeaddress.
- Commas are allowed in pickupPincodes.
```

Notes:

```text
branch_admin_name max-16 rule was not enforced as a character limit because the correct sample value is MUKESHBHAI DHIRUBHAI, which is longer than 16 characters.
The app currently treats the rule as "branch_admin_name should not contain more than 16 digits".
Pincode table existence and active city_id cannot be verified from CSV alone. If Jenkins log says the pincode is not found or city_id is inactive, Ollama is prompted to explain that backend/table check is required.
```

## Why We Changed The Design

Earlier, the website tried to apply one global validation rule to any uploaded file. That was wrong because each Jenkins job can have a different expected CSV format.

The correct design is:

```text
Selected Jenkins job -> stored correct CSV for that job -> validate uploaded CSV -> send findings to Ollama
```

This is more accurate because `User_role_access` rules are only used for `User_role_access`, and future jobs can have their own formats.

## Current Upload Flow

1. Open the website.
2. Select Jenkins job from dropdown.
3. Upload failed Jenkins console log.
4. Upload the CSV input file used in Jenkins.
5. Click `Upload And Explain`.
6. Website stores both files in Docker.
7. Website validates the CSV against the selected job's stored correct CSV.
8. If the CSV has validation problems, the website writes a short plain-English result itself.
9. If the CSV format is correct but the Jenkins log still failed, Ollama explains the Jenkins log.
10. Result is stored in `/data/output`.

## Important Fixes We Added

### 1. Job Dropdown

The website now shows Jenkins jobs from `reference_formats`.

This avoids guessing the job name from the log every time.

### 2. CSV-Only Jenkins Input

The Jenkins input upload now accepts only `.csv`, because Jenkins jobs accept CSV input files.

### 3. Per-Job Validation

The uploaded CSV is compared only with the selected job's correct format.

### 4. Required Fixes

The app now creates deterministic `Required fixes` before Ollama explains anything.

Example for wrong headers:

```text
Uploaded columns:
User_contact, Roel

Required fixes:
- Rename column 'User_contact' to 'User_contact_number'.
- Rename column 'Roel' to 'Role'.
```

For wrong headers and wrong data together:

```text
Uploaded columns:
User_contact, Rort

Data:
900509,admin

Required fixes:
- Rename column 'User_contact' to 'User_contact_number'.
- Rename column 'Rort' to 'Role'.
- Change row 2, column User_contact to exactly 10 digits.
- Change row 2, column Rort from 'admin' to 'ADMIN'.
```

This is handled even when the column header is wrong. The app maps likely wrong headers to the correct headers and still checks the data under them.

### 5. Quoted Role Values

For this uploaded value:

```text
"FE"
```

The app now detects that the real intended value is `FE`, but the quotes are part of the CSV value.

Correct fix:

```text
Remove quotes from row 2, column Role; use FE.
```

### 6. Git Noise Filtering

Some Jenkins logs contain harmless setup lines:

```text
Selected Git installation does not exist. Using Default
The recommended git tool is: NONE
```

Ollama previously suggested fixing Git, which was wrong.

The app now filters normal Jenkins Git checkout noise and focuses on useful lines like:

```text
Invalid role: "FE"
Skipped: 1
Finished: SUCCESS
```

### 7. Better UI

The upload page was restyled:

```text
Cleaner dropdown
No overlap
Better spacing
Separate upload fields
Mobile-safe layout
```

### 8. Simplified Output

The generated result is now intentionally short. It no longer includes long sections like:

```text
Technical evidence
Focused Jenkins log evidence
Full traceback
What to fix / Why duplicate blocks
```

For CSV validation problems, new output looks like this:

```text
Jenkins AI Failure Explanation
==============================

Jenkins log file: #273.txt
Uploaded input CSV: input.csv
Selected Jenkins job: User_role_access
Detected Jenkins status: FAILURE
Created at: ...

Plain-English Explanation
-------------------------
Issue:
The uploaded CSV does not match the correct format for User_role_access.

Fix:
1. Rename column 'User_contact' to 'User_contact_number'.
2. Rename column 'Rort' to 'Role'.
3. Change row 2, column User_contact to exactly 10 digits.
4. Change row 2, column Rort from 'admin' to 'ADMIN'.

Problem found:
- Header mismatch. Expected exactly: User_contact_number, Role.
- Row 2, column User_contact -> User_contact_number: expected 10 digits, found 6.
- Row 2, column Rort -> Role: expected one of ADMIN, AM, FE, found 'admin'.
```

Old result files inside Docker may still show the longer old format. New uploads use the simplified format.

## How To Add A New Jenkins Job

1. Get a correct successful input CSV for that Jenkins job.
2. Use the exact Jenkins job name as the folder name.
3. Create this structure:

```text
reference_formats/<JENKINS_JOB_NAME>/correct_input.csv
```

Example:

```text
reference_formats/New_partner_location_onboarding/correct_input.csv
```

4. Restart the web container:

```bash
docker compose -f docker-compose.local-ollama.yml restart web
```

5. Open the website. The new job should appear in the dropdown.

## Files Changed

Main changed files:

```text
app.py
docker-compose.local-ollama.yml
reference_formats/User_role_access/correct_input.csv
```

Added documentation:

```text
CURRENT_WORKFLOW.md
```

## Notes

- Jenkins itself is not changed.
- Old Docker setup files are not deleted.
- Old uploaded/generated Docker data is not deleted.
- Ollama is used for plain-English explanation, but exact CSV validation is done by the website first.
- This is important because exact row/column validation should not depend only on AI guessing.
- When CSV validation finds problems, the website writes the fix directly instead of relying on Ollama.
