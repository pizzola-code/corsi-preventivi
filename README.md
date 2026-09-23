# Preventivi dei corsi di formazione

Motore che trasforma una richiesta dal catalogo corsi di **spaggiari.eu/corsi-formazione**
nel preventivo intestato alla scuola, spedito per email con il PDF allegato e
l'ufficio MePA in copia.

Sta in un repository pubblico per un motivo pratico: su repo pubblici GitHub non
conta i minuti, quindi il motore puo' essere svegliato ogni minuto e la scuola
riceve il preventivo subito invece di aspettare il giro successivo.

Qui dentro non ci sono credenziali: token e casella di invio arrivano dai
segreti del repository (`HUBSPOT_TOKEN`, `SMTP_CORSI_USER`, `SMTP_CORSI_PASS`).
