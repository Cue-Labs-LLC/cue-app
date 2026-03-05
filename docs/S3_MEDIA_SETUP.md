# S3 media storage (event flyers)

The app uses **django-storages** with S3 for media files (e.g. event flyers) when `AWS_STORAGE_BUCKET_NAME` is set. URLs are **public** (no signed query params). For that to work, the bucket must allow public read.

You do **not** need to create an `event_flyers` folder (or any prefix) in the bucket. S3 has no real folders; uploading a file with key `event_flyers/filename.jpeg` creates that path automatically.

## Symptom: "Access Denied" on flyer images

If event flyers return **Access Denied** (XML `<Code>AccessDenied</Code>`) in the browser, the S3 bucket is not allowing public read. Fix it with one of the two approaches below.

---

## Option A: Bucket policy (recommended)

This allows anyone to **read** objects (e.g. `event_flyers/*`) while uploads still require your IAM credentials. Works even when "Block public access" is on for ACLs, as long as you allow public bucket policies.

### 1. Allow public bucket policies

1. In **AWS Console** → **S3** → bucket `ltv-updater-app-media` (or your `AWS_STORAGE_BUCKET_NAME`).
2. **Permissions** tab → **Block public access** → **Edit**.
3. Uncheck **"Block public access to buckets and objects granted through new public bucket or access point policies"** (or turn off **Block all public access** if you prefer).
4. Save.

### 2. Add bucket policy

In **Permissions** → **Bucket policy** → **Edit**, use a policy that allows public `GetObject` on the bucket contents (e.g. media only or entire bucket):

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME/*"
        }
    ]
}
```

**Important:** The `/*` in `Resource` is required. Without it, the policy applies to the bucket itself, not the objects inside it. `s3:GetObject` only applies to objects, so `arn:aws:s3:::bucket-name/` (no `/*`) will not allow reading files and will still return Access Denied.

Replace `YOUR_BUCKET_NAME` with your bucket name (e.g. `ltv-updater-app-media`).  
To restrict to a prefix (e.g. only event flyers), use:

```json
"Resource": "arn:aws:s3:::YOUR_BUCKET_NAME/event_flyers/*"
```

Save. After this, existing and new uploads should be readable at their public URLs.

### 3. Custom domain (optional)

If you use `AWS_S3_CUSTOM_DOMAIN` (e.g. `ltv-updater-app-media.s3.us-east-1.macom.com`), ensure the domain points to the bucket (e.g. CNAME to the bucket endpoint or CloudFront). The bucket policy above applies regardless of the domain used to access the object.

---

## Option B: Object ACLs (public-read)

The app uses `AWS_DEFAULT_ACL = None` so uploads do not set object ACLs (avoids `AccessControlListNotSupported` when the bucket disallows ACLs). Public read is provided by the **bucket policy** (Option A). If you prefer object ACLs instead, set `AWS_DEFAULT_ACL = 'public-read'` in settings and ensure the bucket allows ACLs. For that to work:

1. **Permissions** → **Block public access** → **Edit**.
2. Uncheck **"Block public access to buckets and objects granted through new access control lists (ACLs)"**.
3. Ensure your IAM user/role has `s3:PutObjectAcl` so uploads can set the ACL.

Objects already uploaded while ACLs were blocked may still be private. Re-upload those flyers or add the **bucket policy** in Option A so all current and future objects are readable without relying on ACLs.

---

## Environment variables (Render / production)

Set in your deployment (e.g. Render env vars):

| Variable | Required | Purpose |
|----------|----------|---------|
| `AWS_STORAGE_BUCKET_NAME` | Yes (for S3) | Bucket name |
| `AWS_ACCESS_KEY_ID` | Yes | IAM access key with `s3:PutObject`, `s3:GetObject`, and (if using ACLs) `s3:PutObjectAcl` |
| `AWS_SECRET_ACCESS_KEY` | Yes | IAM secret key |
| `AWS_S3_REGION_NAME` | Optional | Default `us-east-1` |
| `AWS_S3_CUSTOM_DOMAIN` | Optional | Custom domain for media URLs |

After changing bucket permissions, reload the event page; no redeploy is needed for permission-only fixes.
