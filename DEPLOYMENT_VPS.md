# Οδηγός Ανάπτυξης (Deployment) σε VPS (Hetzner / DigitalOcean)

Ακολουθώντας αυτά τα βήματα, θα στήσεις την εφαρμογή σου σε έναν δικό σου Server (VPS) με κόστος 4-5€ / μήνα, εκμεταλλευόμενος την πλήρη ισχύ του `docker-compose`.

## 1. Δημιουργία του Server
1. Φτιάξε λογαριασμό στο [Hetzner](https://www.hetzner.com/cloud) ή στο [DigitalOcean](https://www.digitalocean.com/).
2. Δημιούργησε ένα νέο **Droplet** (DigitalOcean) ή **Server** (Hetzner).
   - **Λειτουργικό:** Ubuntu 24.04 (ή 22.04) LTS
   - **Μέγεθος:** Το πιο μικρό (Συνήθως 1GB ή 2GB RAM είναι υπέρ-αρκετά).
3. Μόλις δημιουργηθεί, θα σου δοθεί μια **IP** διεύθυνση και ένας κωδικός (ή πρόσβαση μέσω SSH Key).

## 2. Σύνδεση στον Server
Άνοιξε το τερματικό σου (στο PC σου) και γράψε:
```bash
ssh root@Η_IP_ΤΟΥ_SERVER_ΣΟΥ
```
Θα σου ζητήσει τον κωδικό που έλαβες.

## 3. Εγκατάσταση Docker
Μόλις μπεις, τρέξε αυτές τις 2 εντολές για να εγκαταστήσεις το Docker:
```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh
```

## 4. Κατέβασμα της εφαρμογής
Τώρα θα κατεβάσουμε τον κώδικα (από το νέο σου GitHub repository) μέσα στον Server:
```bash
git clone https://github.com/naikos98-ops/GemiLeads.git
cd GemiLeads
```

## 5. Ρύθμιση του `.env`
Ο server χρειάζεται τα "μυστικά" (τα οποία δεν υπάρχουν στο GitHub).
```bash
nano .env
```
Κάνε επικόλληση τα εξής (άλλαξε τις τιμές στα δικά σου):
```env
DJANGO_SECRET_KEY=ενα_μεγαλο_δυσκολο_κλειδι
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=gemileads.gr,Η_IP_ΤΟΥ_SERVER_ΣΟΥ
BASE_URL=http://Η_IP_ΤΟΥ_SERVER_ΣΟΥ:8000

GEMI_API_KEY=το_κλειδι_σου_απο_το_γεμη

EMAIL_HOST=smtp-relay.brevo.com
EMAIL_PORT=587
EMAIL_USE_TLS=1
EMAIL_HOST_USER=το_email_σου@brevo
EMAIL_HOST_PASSWORD=ο_κωδικος_απο_το_brevo
DEFAULT_FROM_EMAIL=Gemi Leads <notifications@gemileads.gr>

POSTGRES_DB=gemileads
POSTGRES_USER=gemileads_user
POSTGRES_PASSWORD=ενας_δυσκολος_κωδικος_βασης
```
Πάτα `Ctrl + O`, `Enter` και μετά `Ctrl + X` για να αποθηκεύσεις και να βγεις.

## 6. Εκκίνηση της Εφαρμογής!
Τώρα δίνουμε εντολή στο Docker να τα σηκώσει όλα (Βάση, Web, Worker):
```bash
docker compose up -d --build
```
*Η πρώτη φορά θα πάρει 2-3 λεπτά γιατί χτίζει το σύστημα.*

Μόλις ολοκληρωθεί, άνοιξε τον browser σου στο:
`http://Η_IP_ΤΟΥ_SERVER_ΣΟΥ:8000`

Η εφαρμογή σου είναι Live! 🎉

*(Όταν αγοράσεις Domain, απλά το συνδέεις σε αυτή την IP).*
