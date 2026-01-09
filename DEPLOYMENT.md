# Render Deployment Guide

This guide walks you through deploying the Event Ticket Order Upload System to Render.

## Prerequisites

1. A Render account (sign up at https://render.com)
2. An AWS account with S3 bucket created (for media file storage)
3. Git repository with your code pushed to GitHub/GitLab/Bitbucket

## Step 1: Prepare AWS S3 Bucket

### Create S3 Bucket

1. Log in to AWS Console
2. Navigate to S3 service
3. Click "Create bucket"
4. Configure bucket:
   - **Bucket name**: Choose a unique name (e.g., `your-app-media`)
   - **Region**: Choose your preferred region (e.g., `us-east-1`)
   - **Block Public Access**: Uncheck "Block all public access" (or configure bucket policy for public read access)
   - **Versioning**: Optional, but recommended
5. Click "Create bucket"

### Configure Bucket Permissions

1. Go to your bucket → **Permissions** tab
2. Under **Bucket Policy**, add the following (replace `your-bucket-name`):

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::your-bucket-name/*"
        }
    ]
}
```

3. Under **CORS**, add the following configuration:

```json
[
    {
        "AllowedHeaders": ["*"],
        "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"],
        "AllowedOrigins": ["*"],
        "ExposeHeaders": []
    }
]
```

### Create IAM User for S3 Access

1. Navigate to IAM → **Users** → **Create user**
2. Name: `render-s3-access`
3. Select "Programmatic access"
4. Attach policy: `AmazonS3FullAccess` (or create custom policy with only necessary permissions)
5. Save the **Access Key ID** and **Secret Access Key** (you'll need these for Render)

## Step 2: Deploy to Render

### Option A: Using render.yaml (Recommended)

1. **Connect Repository**
   - Log in to Render Dashboard
   - Click "New +" → "Blueprint"
   - Connect your Git repository
   - Render will detect `render.yaml` and create services automatically

2. **Configure Environment Variables**
   - Go to your web service → **Environment** tab
   - Add the following environment variables:

```
SECRET_KEY=<generate-a-new-secret-key>
DEBUG=False
ALLOWED_HOSTS=<your-render-service-url>.onrender.com
AWS_ACCESS_KEY_ID=<your-aws-access-key-id>
AWS_SECRET_ACCESS_KEY=<your-aws-secret-access-key>
AWS_STORAGE_BUCKET_NAME=<your-s3-bucket-name>
AWS_S3_REGION_NAME=us-east-1
```

   - **Note**: `DATABASE_URL` is automatically provided by Render when you use the managed PostgreSQL service

3. **Generate Secret Key**
   - Run locally: `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`
   - Or use Render's auto-generated secret key

### Option B: Manual Setup

1. **Create PostgreSQL Database**
   - Click "New +" → "PostgreSQL"
   - Name: `ltv-updater-db`
   - Plan: Starter (or higher for production)
   - Click "Create Database"
   - Note the **Internal Database URL** (auto-provided as `DATABASE_URL`)

2. **Create Web Service**
   - Click "New +" → "Web Service"
   - Connect your Git repository
   - Configure:
     - **Name**: `ltv-updater`
     - **Environment**: `Python 3`
     - **Build Command**: `pip install -r requirements.txt && python manage.py collectstatic --noinput`
     - **Start Command**: `gunicorn ltv_updater.wsgi:application`
     - **Plan**: Starter (or higher)

3. **Set Environment Variables**
   - Go to **Environment** tab
   - Add all variables listed in Option A above
   - Add `DATABASE_URL` (copy from PostgreSQL service)

4. **Configure Health Check**
   - Health Check Path: `/health/`

## Step 3: Initial Database Setup

Once your service is deployed:

1. **Open Render Shell** (or use SSH)
   - Go to your web service → **Shell** tab
   - Or use: `render ssh <service-name>`

2. **Run Migrations**
   ```bash
   python manage.py migrate
   ```

3. **Create Superuser**
   ```bash
   python manage.py createsuperuser
   ```
   - Follow prompts to create admin user

## Step 4: Verify Deployment

1. **Check Health Endpoint**
   - Visit: `https://your-service.onrender.com/health/`
   - Should return "OK"

2. **Test Application**
   - Visit your service URL
   - Log in to admin panel
   - Test file upload functionality
   - Verify files are uploaded to S3

3. **Check Logs**
   - Go to **Logs** tab in Render dashboard
   - Verify no errors during startup
   - Check for any database connection issues

## Step 5: Configure Custom Domain (Optional)

1. Go to your web service → **Settings** → **Custom Domains**
2. Add your domain
3. Follow DNS configuration instructions
4. Update `ALLOWED_HOSTS` environment variable to include your domain

## Environment Variables Reference

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SECRET_KEY` | Django secret key | Auto-generated or custom |
| `DEBUG` | Debug mode | `False` for production |
| `ALLOWED_HOSTS` | Allowed hostnames | `your-service.onrender.com` |
| `AWS_ACCESS_KEY_ID` | AWS access key | From IAM user |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | From IAM user |
| `AWS_STORAGE_BUCKET_NAME` | S3 bucket name | `your-app-media` |
| `AWS_S3_REGION_NAME` | AWS region | `us-east-1` |

### Auto-Provided by Render

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `RENDER` | Set to `true` on Render |
| `RENDER_EXTERNAL_HOSTNAME` | Your service URL |

### Optional Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_S3_CUSTOM_DOMAIN` | Custom S3 domain | None |
| `DJANGO_LOG_LEVEL` | Logging level | `INFO` |

## Troubleshooting

### Database Connection Issues

- Verify `DATABASE_URL` is set correctly
- Check PostgreSQL service is running
- Verify database credentials in Render dashboard

### Static Files Not Loading

- Verify `collectstatic` runs during build (check build logs)
- Check WhiteNoise middleware is in `MIDDLEWARE` list
- Verify `STATIC_ROOT` is set correctly

### Media Files Not Uploading to S3

- Verify AWS credentials are correct
- Check S3 bucket permissions (bucket policy and CORS)
- Verify IAM user has S3 permissions
- Check application logs for AWS errors

### 500 Internal Server Error

- Check Render logs for detailed error messages
- Verify `DEBUG=False` in production (errors won't show details)
- Check database migrations are applied
- Verify all environment variables are set

### Health Check Failing

- Verify `/health/` endpoint is accessible
- Check database connection is working
- Review application logs

## Maintenance

### Running Migrations

1. Open Render Shell
2. Run: `python manage.py migrate`

### Creating New Superuser

1. Open Render Shell
2. Run: `python manage.py createsuperuser`

### Viewing Logs

- Go to your service → **Logs** tab
- Logs are streamed in real-time
- Use search to filter logs

### Updating Application

- Push changes to your Git repository
- Render automatically detects changes and redeploys
- Monitor deployment in **Events** tab

## Security Best Practices

1. **Never commit secrets**: Use environment variables for all sensitive data
2. **Rotate secrets regularly**: Update `SECRET_KEY` and AWS credentials periodically
3. **Use HTTPS**: Render provides SSL certificates automatically
4. **Limit S3 permissions**: Use IAM policies to restrict S3 access to minimum required
5. **Monitor logs**: Regularly check for suspicious activity
6. **Keep dependencies updated**: Regularly update `requirements.txt` packages

## Cost Considerations

- **Starter Plan**: Free tier available (with limitations)
- **PostgreSQL**: Free tier available (with limitations)
- **AWS S3**: Pay-per-use (very low cost for typical usage)
- **Bandwidth**: Included in Render plans

## Support

- Render Documentation: https://render.com/docs
- Django Deployment: https://docs.djangoproject.com/en/stable/howto/deployment/
- AWS S3 Documentation: https://docs.aws.amazon.com/s3/

## Additional Resources

- [Render Python Guide](https://render.com/docs/python)
- [Django on Render](https://render.com/docs/deploy-django)
- [WhiteNoise Documentation](https://whitenoise.readthedocs.io/)
- [django-storages Documentation](https://django-storages.readthedocs.io/)
