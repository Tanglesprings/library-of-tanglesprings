# Deploy the Library of Tanglesprings

The production address is `library.tanglesprings.institute`. GitHub holds the source and runs deployments. A DigitalOcean Droplet runs the same container on every deploy, with Caddy managing TLS. App Platform local storage is replaced during a deployment. Shelf uses SQLite and cover files on a persistent Docker volume.

## Local pilot

```sh
docker build -t tanglesprings-library:pilot .
docker run -d --name tanglesprings-library-pilot \
  -p 127.0.0.1:18889:18888 \
  -v tanglesprings-library-pilot:/data tanglesprings-library:pilot
```

Open `https://localhost:18889/setup` and accept the local certificate for the pilot. Choose the **Everything** profile for individual copy labels, or enable **Collector tools** in Settings after setup. The initial setup creates Undercroft, Southern Annex, and Parlour. Register guests from **Settings → Library → Borrowers**. On **Scan**, select **Lend** or **Return**. Scan the copy label when more than one copy shares a retail code. Use **Add by hand** for board games and puzzles.

Open **Sections** from the Circulation Desk to rename a section, view its titles, or choose **Move titles here** and select a batch. **Scan into section** starts Move mode with that destination selected. Use a copy label when a title has multiple physical copies.

Choose **Print label** beside a section or **Print section labels** to build a batch. **Print on DYMO** queues bold black divider labels for the LabelWriter 450 Turbo on ww-node using the roll configured on that Mac. The worker reports its stock dimensions so a new preview starts with the same size. **Print locally** opens the browser print dialog with adjustable paper dimensions.

The library runs a second launch agent on ww-node using the existing Recovered Garments worker executable, a separate configuration directory, and a library-only pairing key. The library stores a SHA-256 digest of that key in `section_dymo_digest`. The Mac checks `/api/label-printer` over HTTPS and submits stock-sized PDFs to CUPS. A submitted job reports its CUPS ID. If delivery is unconfirmed, check the queue on ww-node before submitting a new job.

## Production host

1. Use a DigitalOcean Linux host with Docker Compose v2 and a persistent disk. Allow ports 80 and 443 for Caddy and restrict SSH to authorized access. Keep the upstream web port on loopback. Set the `library` A record in the `tanglesprings.institute` DigitalOcean zone to the host’s IPv4 address.
2. Give the deployment account access to Docker and ownership of `/opt/tanglesprings-library/releases`. Configure `LIBRARY_DEPLOY_HOST` and `LIBRARY_DEPLOY_USER` as repository variables, and `LIBRARY_DEPLOY_KEY` and `LIBRARY_DEPLOY_KNOWN_HOSTS` as production environment secrets. Verify the host key through the DigitalOcean console. Enable the workflow with the `LIBRARY_DEPLOY_ENABLED` repository variable set to `true`.
3. The initial deploy leaves account setup reachable only over SSH. From your workstation, run `ssh -L 18888:127.0.0.1:18888 <authorized-host>` and open `http://localhost:18888/setup`. Set your admin password there. Returning to the public address should now show the signed-out page. Check `https://library.tanglesprings.institute/api/health` and the exact-revision link at `/source`.
4. Enable host backups and protect them as guest data. Back up the entire `library-data` volume, which includes the SQLite database, covers, and locally generated keys. Use a consistent volume snapshot taken with the library stopped, or use the app’s SQLite backup plus a protected copy of the remaining files. Restore to a separate volume to verify it. The portable catalog archive is for collection transfer. Use a full SQLite backup for guest contact details and copy-specific loan records.

For a manual deploy, run `LIBRARY_REVISION=<commit> LIBRARY_SOURCE_URL=<exact-source-url> docker compose -f compose.production.yml up -d --build --wait` from the matching checkout on the host. Compose retains the same project and data volume across releases. For a failed database change, pair any code rollback with a compatible pre-deploy backup.
