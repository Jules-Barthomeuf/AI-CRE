#!/usr/bin/env python3
import json, os, re, urllib.request, urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler

# ── Clé API Anthropic — vient uniquement de l'environnement, jamais en dur ──
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']  # raises if not set; never hardcode
# ───────────────────────────────────────────────────────────────────────────

PORT = int(os.environ.get('PORT', 8000))
SERVE_DIR = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = """Tu es un expert en investissement immobilier commercial français.
Tu analyses des dossiers d'actifs pour des asset managers et rends un verdict GO / NO-GO selon des critères stricts.

CRITÈRES D'INVESTISSEMENT (non négociables) :
1. RENTABILITÉ
   - Emplacement PRIME (Paris intramuros, Neuilly, Boulogne, Lyon Part-Dieu, Bordeaux centre, Nice Promenade…) : rentabilité brute ≥ 6%
   - Emplacement SECONDAIRE (toutes autres villes et zones : Grenoble, Clermont-Ferrand, Toulouse périphérie, etc.) : rentabilité brute ≥ 7%
   - Calcul : loyer annuel HT HC / prix d'acquisition × 100
   - Si le prix d'acquisition n'est pas fourni, marque ce critère "inconnu"

2. COPROPRIÉTÉ SAINE
   - Aucun conflit en cours (procédures judiciaires, copropriétaires défaillants chroniques, assemblées conflictuelles)
   - Pas d'impayés de charges significatifs
   - Pas de travaux votés importants non budgétés

3. ABSENCE DE FRAIS ANNEXES BLOQUANTS
   - Pas de charges non récupérables importantes sur le local
   - Pas de travaux de copropriété votés à la charge du lot
   - Pas de servitudes ou contraintes non connues

4. DIAGNOSTICS CONFORMES
   - Pas d'amiante (ou déjà traité/confiné avec attestation)
   - Pas de mérules, pas de termites actifs
   - Pas de plomb
   - Pas de problème structurel majeur

5. LOCATAIRE SOLIDE
   - Pas d'impayés de loyer
   - Aucun contentieux avec le bailleur
   - Entreprise solvable (pas de procédure collective en cours)
   - Bail respecté (pas de violations de clauses)

RÈGLES DE VERDICT :
- GO : tous les critères bloquants sont conformes (ou inconnus mais non bloquants)
- ATTENTION : 1 critère avec point d'attention non bloquant, ou informations insuffisantes sur 1 critère bloquant
- NO_GO : au moins 1 critère bloquant est KO

RÈGLES JSON CRITIQUES :
- Retourne UNIQUEMENT un objet JSON valide, sans markdown ni backticks
- Dans les valeurs string : représente les sauts de ligne par \\n
- Ne mets jamais de vrai retour à la ligne dans une valeur string
- Tous les champs string sur une seule ligne logique"""

USER_PROMPT_TEMPLATE = """Analyse ce dossier d'investissement immobilier et retourne UNIQUEMENT un objet JSON valide.

Structure exacte attendue :
{{
  "verdict": "GO",
  "synthese": "Résumé exécutif en 3-4 phrases sans retour à la ligne.",
  "rentabilite": {{
    "taux": 6.5,
    "loyerAnnuel": "78 000 €",
    "prixAcquisition": "1 200 000 €",
    "emplacement": "prime",
    "seuilRequis": 6.0,
    "conforme": true,
    "detail": "Détail du calcul en une ligne."
  }},
  "criteres": [
    {{
      "id": "rentabilite",
      "label": "Rentabilité minimale",
      "statut": "ok",
      "bloquant": true,
      "detail": "Explication courte en une ligne."
    }},
    {{
      "id": "copropriete",
      "label": "Copropriété saine",
      "statut": "ok",
      "bloquant": true,
      "detail": "Explication."
    }},
    {{
      "id": "charges",
      "label": "Absence de frais annexes",
      "statut": "ok",
      "bloquant": false,
      "detail": "Explication."
    }},
    {{
      "id": "diagnostics",
      "label": "Diagnostics conformes",
      "statut": "inconnu",
      "bloquant": true,
      "detail": "Explication."
    }},
    {{
      "id": "locataire",
      "label": "Locataire solide",
      "statut": "ok",
      "bloquant": true,
      "detail": "Explication."
    }}
  ],
  "risques": ["Risque 1 en une ligne.", "Risque 2."],
  "points_positifs": ["Point positif 1.", "Point positif 2."],
  "documents_manquants": ["Document manquant 1", "Document manquant 2"]
}}

"verdict" = GO | ATTENTION | NO_GO
"statut" = ok | ko | attention | inconnu
"emplacement" = prime | secondaire
Inclure impérativement les 5 critères dans l'ordre.
"documents_manquants" = liste des documents absents du dossier qui auraient été utiles à l'analyse.

DOSSIER À ANALYSER :
{documents}

INFORMATIONS COMPLÉMENTAIRES :
{additional}
"""

DOC_TYPE_LABELS = {
    'bail': 'BAIL COMMERCIAL',
    'ag': "PROCÈS-VERBAL D'ASSEMBLÉE GÉNÉRALE",
    'copro': 'RÈGLEMENT DE COPROPRIÉTÉ',
    'quittances': 'QUITTANCES DE LOYER',
    'diag': 'DIAGNOSTIC IMMOBILIER (DDT)',
    'finance': 'DONNÉES FINANCIÈRES',
    'autre': 'DOCUMENT COMPLÉMENTAIRE',
}

def build_prompt(documents, additional_info):
    docs_text = ''
    for i, doc in enumerate(documents, 1):
        label = DOC_TYPE_LABELS.get(doc.get('type', 'autre'), 'DOCUMENT')
        filename = doc.get('filename', f'document_{i}')
        text = doc.get('text', '')[:50000]
        docs_text += f'\n\n{"="*60}\n[Document {i} — {label}]\nFichier : {filename}\n{"="*60}\n{text}'

    return USER_PROMPT_TEMPLATE.format(
        documents=docs_text or 'Aucun document fourni.',
        additional=additional_info or 'Aucune information complémentaire.'
    )

def extract_and_fix_json(text):
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        raise ValueError('Aucun JSON trouvé dans la réponse')
    raw = match.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Repair literal newlines inside string values
    fixed = re.sub(
        r'("(?:[^"\\]|\\.)*")',
        lambda m: m.group(0).replace('\n', '\\n').replace('\r', '').replace('\t', ' '),
        raw
    )
    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        raise ValueError(f'JSON invalide : {e}')

class Handler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != '/api/analyze':
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length))
            documents = body.get('documents', [])
            additional = body.get('additionalInfo', '')

            prompt = build_prompt(documents, additional)
            print(f'[Bail Bye] Analyse — {len(documents)} doc(s), prompt {len(prompt)} chars')

            payload = json.dumps({
                'model': 'claude-sonnet-4-6',
                'max_tokens': 8192,
                'system': SYSTEM_PROMPT,
                'messages': [{'role': 'user', 'content': prompt}],
            }).encode()

            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': ANTHROPIC_API_KEY,
                    'anthropic-version': '2023-06-01',
                }
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                api_resp = json.loads(resp.read())

            claude_text = api_resp['content'][0]['text']
            parsed = extract_and_fix_json(claude_text)
            print(f'[Bail Bye] Verdict : {parsed.get("verdict", "?")}')

            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'content': [{'text': json.dumps(parsed, ensure_ascii=False)}]
            }).encode('utf-8'))

        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f'[Bail Bye] Erreur HTTP {e.code}: {err_body[:200]}')
            self.send_response(e.code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as ex:
            print(f'[Bail Bye] ERREUR: {ex}')
            self.send_response(500)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': {'message': str(ex)}}).encode())

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def log_message(self, fmt, *args):
        pass  # Logs gérés manuellement ci-dessus

if __name__ == '__main__':
    import socket
    os.chdir(SERVE_DIR)
    class DualHTTPServer(HTTPServer):
        address_family = socket.AF_INET6
    try:
        httpd = DualHTTPServer(('::', PORT), Handler)
        print(f'✓ Bail Bye — http://localhost:{PORT}/bail-bye.html')
    except OSError:
        httpd = HTTPServer(('0.0.0.0', PORT), Handler)
        print(f'✓ Bail Bye — http://127.0.0.1:{PORT}/bail-bye.html')
    httpd.serve_forever()
