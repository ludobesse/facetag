"""Étape 4 : application de bureau FaceTag (PySide6).

Gauche  : À confirmer (par personne) · Groupes d'inconnus · Inconnus isolés · Personnes · Ignorés
Centre  : vignettes des visages (sélection multiple : clic, Maj+clic, Ctrl+clic, Ctrl+A)
Bas     : actions sur la sélection

Barre du haut : Détecter les visages dans un dossier · Recalculer les suggestions · Écrire les métadonnées
Un nom absent de Google Contacts peut être créé dans Google Contacts (ou seulement dans FaceTag).

Raccourcis : Entrée = confirmer la suggestion · Suppr = ignorer · Ctrl+N = saisir un nom
             double-clic = ouvrir la photo

Usage : python -m facetag.app
"""

import os
import subprocess
import sys
import threading

from PySide6.QtCore import (QObject, QRunnable, QSettings, QSize, Qt, QThread, QThreadPool, QTimer, QUrl,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPen,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QCompleter, QFrame, QGroupBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import actions, config, contacts, contacts_file, db, exiftool_setup, match, scan, thumbs, write

PAGE = 1000          # nombre max de vignettes affichées par vue
ICON = 128


# ------------------------------------------------------------------ icônes

def contact_icon(linked: bool) -> QIcon:
    """Petite icône « contact » : pleine (bleue) si la personne est liée à Google Contacts,
    simple contour gris sinon."""
    px = QPixmap(32, 32)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    if linked:
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#1a73e8")))
        p.drawEllipse(1, 1, 30, 30)
        p.setBrush(QBrush(Qt.white))
        p.drawEllipse(11, 6, 10, 10)          # tête
        p.drawChord(6, 17, 20, 18, 0, 180 * 16)  # épaules
    else:
        p.setPen(QPen(QColor("#9aa0a6"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(2, 2, 28, 28)
        p.drawEllipse(12, 7, 8, 8)
        p.drawArc(8, 18, 16, 14, 0, 180 * 16)
    p.end()
    return QIcon(px)


# ------------------------------------------------------------------ tâches de fond

class Worker(QThread):
    """Exécute une fonction longue (suggestions, écriture) sans figer l'interface."""
    progressed = Signal(int, int)
    logged = Signal(str)
    done = Signal(object, str)   # résultat, erreur ("" si OK)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def run(self):
        try:
            res = self.fn(*self.args, progress=self.progressed.emit, **self.kwargs)
            self.done.emit(res, "")
        except Exception as exc:  # noqa: BLE001 — remonté à l'utilisateur
            self.done.emit(None, str(exc))


class ThumbSignals(QObject):
    ready = Signal(int, int, str)  # génération, face_id, chemin de la vignette


class ThumbTask(QRunnable):
    def __init__(self, gen, face_id, photo, box, signals):
        super().__init__()
        self.gen, self.face_id, self.photo, self.box, self.signals = gen, face_id, photo, box, signals

    def run(self):
        p = thumbs.ensure(self.face_id, self.photo, *self.box)
        self.signals.ready.emit(self.gen, self.face_id, str(p) if p else "")


# ------------------------------------------------------------------ fenêtre principale

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FaceTag")
        self.resize(1400, 900)
        self.conn = db.connect()
        self.kind = None          # vue courante : (type, clé)
        self.gen = 0              # génération de vue (ignore les vignettes d'une ancienne vue)
        self.items: dict[int, QListWidgetItem] = {}
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(6)
        self.tsig = ThumbSignals()
        self.tsig.ready.connect(self._thumb_ready)
        self.worker = None
        # Apprentissage continu : 1,5 s après votre dernière action, les suggestions, groupes et
        # fausses détections probables sont recalculés en arrière-plan avec ce que vous avez saisi.
        self.learn_worker = None
        self.learn_timer = QTimer(self)
        self.learn_timer.setSingleShot(True)
        self.learn_timer.setInterval(1500)
        self.learn_timer.timeout.connect(self._learn)

        # Barre d'outils
        tb = QToolBar()
        self.addToolBar(tb)
        self.settings = QSettings("FaceTag", "FaceTag")
        self.btn_scan = QPushButton("Détecter les visages dans un dossier…")
        self.btn_scan.clicked.connect(self.open_scan)
        self.btn_match = QPushButton("Recalculer les suggestions")
        self.btn_match.clicked.connect(self.run_match)
        self.btn_write = QPushButton("Écrire les métadonnées…")
        self.btn_write.clicked.connect(self.open_write)
        btn_settings = QPushButton("⚙ Paramètres")
        btn_settings.clicked.connect(self.open_settings)
        btn_refresh = QPushButton("Actualiser")
        btn_refresh.clicked.connect(self.refresh_tree)
        for b in (self.btn_scan, self.btn_match, self.btn_write, btn_refresh, btn_settings):
            tb.addWidget(b)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(250)
        self.progress.hide()
        tb.addWidget(self.progress)

        # Arborescence
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(16, 16))
        self.ico_linked, self.ico_local = contact_icon(True), contact_icon(False)
        self.tree.itemSelectionChanged.connect(self._tree_changed)

        # Vignettes
        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(QSize(ICON, ICON))
        self.grid.setGridSize(QSize(ICON + 24, ICON + 44))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.itemSelectionChanged.connect(self._update_buttons)
        self.grid.itemDoubleClicked.connect(self._open_photo)
        self.header = QLabel()

        # Actions
        self.sel_label = QLabel()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Nom (Google Contacts ou nouveau)…  Ctrl+N")
        self.name_edit.setMinimumWidth(280)
        self.name_edit.returnPressed.connect(self.do_assign)
        self.b_confirm = QPushButton("✔ Confirmer  (Entrée)")
        self.b_assign = QPushButton("Nommer")
        self.b_reject = QPushButton("✖ Pas cette personne")
        self.b_ignore = QPushButton("Ignorer  (Suppr)")
        self.b_restore = QPushButton("Rétablir")
        self.b_isface = QPushButton("C'est bien un visage")
        self.b_isface.clicked.connect(self.do_is_face)
        self.b_false = QPushButton("Pas un visage  (Maj+Suppr)")
        self.b_false.setToolTip("Fausse détection (objet, motif, statue, reflet…) : ne plus la proposer")
        self.b_false.clicked.connect(self.do_not_face)
        self.b_open = QPushButton("Ouvrir la photo")
        self.b_google = QPushButton("Créer le contact Google")
        self.b_google.setToolTip("Cette personne n'existe que dans FaceTag : l'ajouter à Google Contacts")
        self.b_google.clicked.connect(self.google_for_current_person)
        self.b_split = QPushButton("Scinder ce groupe")
        self.b_split.setToolTip("Le groupe mélange plusieurs personnes : le couper en deux sous-groupes\n"
                                "de visages qui se ressemblent (répétable si un sous-groupe reste mélangé)")
        self.b_split.clicked.connect(self.do_split)
        self.b_ungroup = QPushButton("Retirer du groupe")
        self.b_ungroup.setToolTip("Sortir les visages sélectionnés du groupe (ils passent dans « Inconnus isolés »)")
        self.b_ungroup.clicked.connect(self.do_ungroup)
        self.b_photo = QPushButton("📷 Photo du contact Google")
        self.b_photo.setToolTip("Utiliser ce visage comme photo du contact dans Google Contacts")
        self.b_photo.clicked.connect(self.push_contact_photo)
        self.b_confirm.clicked.connect(self.do_confirm)
        self.b_assign.clicked.connect(self.do_assign)
        self.b_reject.clicked.connect(self.do_reject)
        self.b_ignore.clicked.connect(self.do_ignore)
        self.b_restore.clicked.connect(self.do_restore)
        self.b_open.clicked.connect(lambda: self._open_photo(self.grid.currentItem()))

        bar = QHBoxLayout()
        bar.addWidget(self.sel_label)
        bar.addStretch()
        for w in (self.b_confirm, self.name_edit, self.b_assign, self.b_reject, self.b_ignore, self.b_false,
                  self.b_isface,
                  self.b_restore, self.b_split, self.b_ungroup, self.b_google, self.b_photo, self.b_open):
            bar.addWidget(w)

        center = QWidget()
        v = QVBoxLayout(center)
        v.addWidget(self.header)
        self.hint_bar = QWidget()
        self.hint_lay = QHBoxLayout(self.hint_bar)
        self.hint_lay.setContentsMargins(0, 0, 0, 0)
        self.hint_bar.hide()
        v.addWidget(self.hint_bar)
        v.addWidget(self.grid, 1)
        v.addLayout(bar)

        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(center)
        split.setSizes([330, 1070])
        self.setCentralWidget(split)

        QShortcut(QKeySequence(Qt.Key_Delete), self.grid, self.do_ignore)
        QShortcut(QKeySequence("Shift+Delete"), self.grid, self.do_not_face)
        QShortcut(QKeySequence(Qt.Key_Return), self.grid, self.do_confirm)
        QShortcut(QKeySequence(Qt.Key_Enter), self.grid, self.do_confirm)
        QShortcut(QKeySequence("Ctrl+N"), self, self.name_edit.setFocus)

        self.refresh_names()
        self.refresh_tree()
        self._update_buttons()
        QTimer.singleShot(300, self._startup_checks)

    def _startup_checks(self):
        self.check_exiftool()
        if contacts.client_info() is None and not self.q("SELECT 1 FROM persons LIMIT 1"):
            self.open_settings()  # premier lancement : choisir comment importer les contacts

    # ---------------------------------------------------------------- données

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def refresh_names(self):
        names = sorted({r["name"] for r in self.q("SELECT name FROM persons")}, key=str.casefold)
        comp = QCompleter(names, self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.setMaxVisibleItems(15)
        self.name_edit.setCompleter(comp)

    def refresh_tree(self, reload: bool = True):
        """Reconstruit la liste de gauche. reload=False : met à jour les compteurs sans recharger
        les vignettes affichées (utilisé après l'apprentissage en arrière-plan)."""
        current = self.kind
        self.tree.blockSignals(True)
        self.tree.clear()

        def node(parent, text, kind=None, linked=None):
            it = QTreeWidgetItem(parent, [text])
            it.setData(0, Qt.UserRole, kind)
            if linked is not None:
                it.setIcon(0, self.ico_linked if linked else self.ico_local)
                it.setToolTip(0, "Lié à Google Contacts" if linked
                              else "Pas dans Google Contacts (bouton « Créer le contact Google »)")
            return it

        sugg = self.q("SELECT p.id, p.name, p.google_resource, COUNT(*) n FROM faces f "
                      "JOIN persons p ON p.id = f.person_id "
                      "WHERE f.status = 'suggested' GROUP BY p.id ORDER BY n DESC, p.name")
        top = node(self.tree, f"À confirmer ({sum(r['n'] for r in sugg)})")
        for r in sugg:
            node(top, f"{r['name']}  ({r['n']})", ("sugg", r["id"]), bool(r["google_resource"]))

        groups = self.q(
            "SELECT f.cluster_id, COUNT(*) n, pe.name hint, h.score FROM faces f "
            "LEFT JOIN group_hints h ON h.cluster_id = f.cluster_id AND h.rank = 0 AND h.score >= ? "
            "LEFT JOIN persons pe ON pe.id = h.person_id "
            "WHERE f.status = 'unknown' AND f.cluster_id IS NOT NULL "
            "GROUP BY f.cluster_id ORDER BY n DESC LIMIT 500", (match.GROUP_HINT_MIN,))
        n_hint = sum(1 for r in groups if r["hint"])
        top = node(self.tree, f"Groupes d'inconnus ({len(groups)}"
                              + (f", {n_hint} avec suggestion)" if n_hint else ")"))
        for i, r in enumerate(groups, 1):
            label = f"Groupe {i}  ({r['n']} visages)"
            if r["hint"]:
                label += f"  → {r['hint']} ?"
            it = node(top, label, ("group", r["cluster_id"]))
            if r["hint"]:
                it.setToolTip(0, f"Ressemble à {r['hint']} (score moyen {r['score']:.2f})")

        n_iso = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'unknown' AND photo_id IS NOT NULL "
                       "AND cluster_id IS NULL AND fp_score IS NULL")[0]["n"]
        node(self.tree, f"Inconnus isolés ({n_iso})", ("isolated", None))

        people = self.q("SELECT p.id, p.name, p.google_resource, p.source, COUNT(*) n FROM faces f "
                        "JOIN persons p ON p.id = f.person_id "
                        "WHERE f.status = 'confirmed' AND f.photo_id IS NOT NULL GROUP BY p.id "
                        "ORDER BY p.name COLLATE NOCASE")
        top = node(self.tree, f"Personnes ({len(people)})")
        for r in people:
            it = node(top, f"{r['name']}  ({r['n']})", ("person", r["id"]), bool(r["google_resource"]))
            if r["source"] == "file":
                it.setToolTip(0, "Importé d'un fichier de contacts (non relié à Google en direct)")

        n_ign = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'ignored' AND false_positive = 0")[0]["n"]
        node(self.tree, f"Ignorés ({n_ign})", ("ignored", None))
        n_pfp = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'unknown' AND fp_score IS NOT NULL")[0]["n"]
        if n_pfp:
            node(self.tree, f"Fausses détections probables ({n_pfp})", ("probable_fp", None))
        n_fp = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'ignored' AND false_positive = 1")[0]["n"]
        node(self.tree, f"Fausses détections ({n_fp})", ("false", None))

        n_write = self.q("SELECT COUNT(*) n FROM photos WHERE needs_write = 1")[0]["n"]
        self.btn_write.setText(f"Écrire les métadonnées… ({n_write})")

        self.tree.expandToDepth(0)
        self.tree.blockSignals(False)
        # resélectionner la vue courante si elle existe encore
        if current:
            it = self._find_node(current)
            if it:
                if reload:
                    self.tree.setCurrentItem(it)
                else:
                    self.tree.blockSignals(True)
                    self.tree.setCurrentItem(it)
                    self.tree.blockSignals(False)
                return
            if not reload:
                return  # la vue affichée reste à l'écran ; elle sera rafraîchie au prochain clic
        self.kind = None
        self.grid.clear()
        self.header.setText("Choisissez une catégorie à gauche.")

    def _find_node(self, kind):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            it = stack.pop()
            if it.data(0, Qt.UserRole) == kind:
                return it
            stack.extend(it.child(i) for i in range(it.childCount()))
        return None

    def _tree_changed(self):
        items = self.tree.selectedItems()
        kind = items[0].data(0, Qt.UserRole) if items else None
        if kind:
            self.load_faces(tuple(kind))

    def load_faces(self, kind):
        self.kind = kind
        self.gen += 1
        t, key = kind
        base = ("SELECT f.id, f.x, f.y, f.w, f.h, f.similarity, ph.path, pe.name, pe.id pid, "
                "pe.google_resource gres FROM faces f "
                "JOIN photos ph ON ph.id = f.photo_id LEFT JOIN persons pe ON pe.id = f.person_id ")
        where = {
            "sugg": ("WHERE f.status = 'suggested' AND f.person_id = ? ORDER BY f.similarity DESC", (key,)),
            "group": ("WHERE f.status = 'unknown' AND f.cluster_id = ? ORDER BY f.score DESC", (key,)),
            "isolated": ("WHERE f.status = 'unknown' AND f.cluster_id IS NULL AND f.fp_score IS NULL "
                         "ORDER BY f.score DESC", ()),
            "probable_fp": ("WHERE f.status = 'unknown' AND f.fp_score IS NOT NULL ORDER BY f.fp_score DESC", ()),
            "person": ("WHERE f.status = 'confirmed' AND f.person_id = ? ORDER BY ph.path", (key,)),
            "ignored": ("WHERE f.status = 'ignored' AND f.false_positive = 0 ORDER BY ph.path", ()),
            "false": ("WHERE f.status = 'ignored' AND f.false_positive = 1 ORDER BY ph.path", ()),
        }[t]
        rows = self.q(base + where[0] + f" LIMIT {PAGE + 1}", where[1])
        more = len(rows) > PAGE
        rows = rows[:PAGE]

        self.grid.clear()
        self.items.clear()
        placeholder = QPixmap(ICON, ICON)
        placeholder.fill(Qt.lightGray)
        ph_icon = QIcon(placeholder)
        for r in rows:
            label = r["name"] or ""
            if t == "sugg":
                label = f"{r['similarity']:.2f}"
            it = QListWidgetItem(ph_icon, label)
            it.setData(Qt.UserRole, r["id"])
            it.setData(Qt.UserRole + 1, r["path"])
            it.setData(Qt.UserRole + 2, (r["pid"], r["gres"], r["name"], r["x"], r["y"], r["w"], r["h"]))
            it.setToolTip(r["path"])
            self.grid.addItem(it)
            self.items[r["id"]] = it
            tp = thumbs.path(r["id"])
            if tp.exists():
                it.setIcon(QIcon(str(tp)))
            else:
                self.pool.start(ThumbTask(self.gen, r["id"], r["path"],
                                          (r["x"], r["y"], r["w"], r["h"]), self.tsig))

        titles = {"sugg": "Suggestions — vérifiez puis confirmez (score de ressemblance sous chaque visage)",
                  "group": "Visages similaires non identifiés — sélectionnez-les et donnez un nom",
                  "isolated": "Visages non identifiés",
                  "person": "Visages confirmés",
                  "ignored": "Visages ignorés",
                  "false": "Fausses détections (pas des visages)",
                  "probable_fp": "Ressemblent à des fausses détections déjà signalées — à vérifier "
                                 "(« Pas un visage » ou « C'est bien un visage »)"}
        extra = f" — {PAGE} premiers affichés" if more else ""
        self.header.setText(f"<b>{titles[t]}</b> · {len(rows)} visage(s){extra}")
        if t in ("sugg", "probable_fp"):
            self.grid.selectAll()   # cas le plus fréquent : tout valider après vérification
        self._show_hints(key if t == "group" else None)
        self._update_buttons()

    def _show_hints(self, cluster_id):
        """Boutons « nom suggéré » pour un groupe d'inconnus."""
        while self.hint_lay.count():
            w = self.hint_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        hints = [] if cluster_id is None else self.q(
            "SELECT h.person_id, h.score, pe.name, pe.google_resource FROM group_hints h "
            "JOIN persons pe ON pe.id = h.person_id WHERE h.cluster_id = ? AND h.score >= 0.15 "
            "ORDER BY h.rank", (cluster_id,))
        if not hints:
            self.hint_bar.hide()
            return
        self.hint_lay.addWidget(QLabel("Ce groupe ressemble à :"))
        for h in hints:
            b = QPushButton(f"{h['name']}  ({h['score']:.2f})")
            b.setIcon(self.ico_linked if h["google_resource"] else self.ico_local)
            strong = h["score"] >= match.GROUP_HINT_MIN
            b.setToolTip("Attribuer ce nom aux visages sélectionnés (ou à tout le groupe si aucune sélection)"
                         + ("" if strong else "\nRessemblance faible : vérifiez bien"))
            if not strong:
                b.setStyleSheet("color: gray;")
            b.clicked.connect(lambda _=False, pid=h["person_id"], name=h["name"]: self._assign_hint(pid, name))
            self.hint_lay.addWidget(b)
        self.hint_lay.addStretch()
        self.hint_bar.show()

    def _assign_hint(self, person_id, name):
        ids = self.selected_ids() or [self.grid.item(i).data(Qt.UserRole) for i in range(self.grid.count())]
        if not ids:
            return
        if QMessageBox.question(self, "Nommer le groupe",
                                f"Attribuer « {name} » à {len(ids)} visage(s) ?") != QMessageBox.Yes:
            return
        actions.assign(self.conn, ids, person_id)
        self._after(ids)

    def _thumb_ready(self, gen, face_id, path):
        if gen != self.gen or not path:
            return
        it = self.items.get(face_id)
        if it is not None:
            it.setIcon(QIcon(path))

    # ---------------------------------------------------------------- actions

    def selected_ids(self):
        return [it.data(Qt.UserRole) for it in self.grid.selectedItems()]

    def _update_buttons(self):
        n = len(self.grid.selectedItems())
        self.sel_label.setText(f"Sélection : {n}")
        t = self.kind[0] if self.kind else None
        self.b_confirm.setVisible(t == "sugg")
        self.b_reject.setVisible(t in ("sugg", "person"))
        self.b_reject.setText("✖ Pas cette personne" if t == "sugg" else "Retirer le nom")
        self.b_restore.setVisible(t in ("ignored", "false"))
        self.b_ignore.setVisible(t not in ("ignored", "false"))
        self.b_false.setVisible(t != "false")
        self.b_isface.setVisible(t == "probable_fp")
        self.b_isface.setEnabled(n > 0)
        self.b_false.setEnabled(n > 0)
        for b in (self.b_confirm, self.b_assign, self.b_reject, self.b_ignore, self.b_restore):
            b.setEnabled(n > 0)
        self.b_open.setEnabled(self.grid.currentItem() is not None)
        unlinked = False
        if t == "person":
            row = self.q("SELECT google_resource FROM persons WHERE id = ?", (self.kind[1],))
            unlinked = bool(row) and not row[0]["google_resource"]
        self.b_google.setVisible(unlinked)
        self.b_photo.setVisible(t == "person" and not unlinked)
        self.b_split.setVisible(t == "group")
        self.b_ungroup.setVisible(t == "group")
        self.b_ungroup.setEnabled(n > 0)
        self.b_photo.setEnabled(n == 1)

    def _after(self, ids):
        for fid in ids:
            it = self.items.pop(fid, None)
            if it is not None:
                self.grid.takeItem(self.grid.row(it))
        self.refresh_tree()
        self.learn_timer.start()   # apprendre de cette action (relancé à chaque clic)

    def _learn(self):
        if self.learn_worker and self.learn_worker.isRunning():
            self.learn_timer.start()  # un calcul est en cours : on réessaiera juste après
            return
        use = self.use_contact_photos()
        before = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'suggested'")[0]["n"]
        self.statusBar().showMessage("Apprentissage à partir de vos choix…")

        def done(res, err):
            if err:
                self.statusBar().showMessage(f"Apprentissage interrompu : {err}", 10000)
                return
            after = self.q("SELECT COUNT(*) n FROM faces WHERE status = 'suggested'")[0]["n"]
            self.refresh_tree(reload=False)
            self.statusBar().showMessage(
                f"Appris de vos choix — suggestions à confirmer : {after} ({after - before:+d}). "
                "Cliquez sur une catégorie pour voir les nouveautés.", 10000)
        self.learn_worker = Worker(lambda progress: match.run(use_contact_photos=use))
        self.learn_worker.done.connect(done)
        self.learn_worker.start()

    def do_confirm(self):
        if self.kind and self.kind[0] == "sugg":
            ids = self.selected_ids()
            actions.confirm_suggestions(self.conn, ids)
            self._after(ids)

    def do_assign(self):
        ids = self.selected_ids()
        name = " ".join(self.name_edit.text().split())
        if not ids:
            return
        if not name:
            self.name_edit.setFocus()
            return
        exists = self.q("SELECT 1 FROM persons WHERE name = ? COLLATE NOCASE", (name,))
        to_google = False
        if not exists:
            box = QMessageBox(self)
            box.setWindowTitle("Nouvelle personne")
            box.setText(f"« {name} » n'est pas dans vos contacts Google.")
            b_g = box.addButton("Créer aussi dans Google Contacts", QMessageBox.AcceptRole)
            b_l = box.addButton("Seulement dans FaceTag", QMessageBox.ActionRole)
            box.addButton("Annuler", QMessageBox.RejectRole)
            box.setDefaultButton(b_g)
            box.exec()
            if box.clickedButton() is b_g:
                to_google = True
            elif box.clickedButton() is not b_l:
                return
        pid = actions.get_or_create_person(self.conn, name)
        actions.assign(self.conn, ids, pid)
        self.name_edit.clear()
        if not exists:
            self.refresh_names()
        if self.kind and self.kind[0] == "person" and self.kind[1] == pid:
            self.refresh_tree()
            self.learn_timer.start()
        else:
            self._after(ids)
        if to_google:
            self.create_google_contact(pid, name)

    # ---------------------------------------------------------------- Google Contacts

    def push_contact_photo(self):
        items = self.grid.selectedItems()
        if len(items) != 1:
            return
        it = items[0]
        pid, resource, name, x, y, w, h = it.data(Qt.UserRole + 2)
        if not resource:
            return
        jpeg = thumbs.portrait_jpeg(it.data(Qt.UserRole + 1), x, y, w, h)
        if not jpeg:
            QMessageBox.warning(self, "Photo du contact", "Impossible de lire la photo d'origine.")
            return
        pix = QPixmap()
        pix.loadFromData(jpeg)
        box = QMessageBox(self)
        box.setWindowTitle("Photo du contact Google")
        box.setIconPixmap(pix.scaled(240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        box.setText(f"Remplacer la photo de <b>{name}</b> dans Google Contacts par celle-ci ?")
        ok = box.addButton("Remplacer", QMessageBox.AcceptRole)
        box.addButton("Annuler", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            return
        self.statusBar().showMessage(
            "Envoi de la photo… (si le navigateur s'ouvre, autorisez l'accès en écriture)")

        def done(res, err):
            if err:
                QMessageBox.warning(self, "Google Contacts", f"La photo n'a pas été envoyée :\n{err}")
            else:
                self.statusBar().showMessage(f"Photo de {name} mise à jour dans Google Contacts", 10000)
        self._start(lambda progress: contacts.update_contact_photo(resource, jpeg, pid), done)

    def google_for_current_person(self):
        if self.kind and self.kind[0] == "person":
            row = self.q("SELECT name FROM persons WHERE id = ?", (self.kind[1],))
            if row:
                self.create_google_contact(self.kind[1], row[0]["name"])

    def create_google_contact(self, person_id, name):
        dlg = ContactDialog(self, name)
        if dlg.exec() != QDialog.Accepted:
            return
        given, family = dlg.values()
        self.statusBar().showMessage(
            "Création du contact Google… (si le navigateur s'ouvre, autorisez l'accès en écriture)")

        def done(res, err):
            if err:
                QMessageBox.warning(self, "Google Contacts", f"Le contact n'a pas été créé :\n{err}")
                return
            self.statusBar().showMessage(f"Contact Google créé : {res[1]}", 10000)
            self.refresh_names()
            self.refresh_tree()
        self._start(lambda progress: contacts.create_contact(given, family, person_id), done)

    def do_reject(self):
        ids = self.selected_ids()
        actions.reject(self.conn, ids)
        self._after(ids)

    def do_ignore(self):
        if self.kind and self.kind[0] in ("ignored", "false"):
            return
        ids = self.selected_ids()
        actions.ignore(self.conn, ids)
        self._after(ids)

    def do_split(self):
        if not (self.kind and self.kind[0] == "group"):
            return
        r = match.split_group(self.conn, self.kind[1], self.use_contact_photos())
        self.statusBar().showMessage(
            f"Groupe de {r['faces']} visages scindé : {r['groups']} groupe(s), "
            f"{r['isolated']} visage(s) isolé(s). Encore mélangé ? Scindez à nouveau.", 15000)
        # afficher le plus grand des sous-groupes obtenus
        self.kind = ("group", r["largest"]) if r.get("largest") else None
        self.refresh_tree()
        self.learn_timer.start()

    def do_ungroup(self):
        ids = self.selected_ids()
        match.remove_from_group(self.conn, ids)
        self._after(ids)

    def do_is_face(self):
        ids = self.selected_ids()
        actions.is_a_face(self.conn, ids)
        self._after(ids)

    def do_not_face(self):
        if self.kind and self.kind[0] == "false":
            return
        ids = self.selected_ids()
        actions.not_a_face(self.conn, ids)
        self._after(ids)

    def do_restore(self):
        ids = self.selected_ids()
        actions.restore(self.conn, ids)
        self._after(ids)

    def _open_photo(self, item):
        if item is None:
            return
        path = item.data(Qt.UserRole + 1)
        if os.name == "nt":
            os.startfile(path)  # visionneuse Windows par défaut
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ---------------------------------------------------------------- tâches longues

    def _start(self, fn, on_done, **kwargs):
        if self.worker and self.worker.isRunning():
            return
        self.progress.setValue(0)
        self.progress.show()
        self.btn_match.setEnabled(False)
        self.btn_write.setEnabled(False)
        self.worker = Worker(fn, **kwargs)
        self.worker.progressed.connect(lambda d, t: (self.progress.setMaximum(max(t, 1)),
                                                     self.progress.setValue(d)))
        self.worker.done.connect(on_done)
        self.worker.done.connect(lambda *_: (self.progress.hide(), self.btn_match.setEnabled(True),
                                             self.btn_write.setEnabled(True)))
        self.worker.start()

    def run_match(self):
        def done(res, err):
            if err:
                QMessageBox.warning(self, "Suggestions", err)
            else:
                self.statusBar().showMessage(res, 15000)
            self.refresh_tree()
        use = self.use_contact_photos()
        self._start(lambda progress: match.run(progress, use_contact_photos=use), done)

    def open_scan(self):
        start = self.settings.value("last_folder", "")
        folder = QFileDialog.getExistingDirectory(self, "Dossier de photos à analyser", start)
        if not folder:
            return
        folder = os.path.normpath(folder)
        self.settings.setValue("last_folder", folder)
        ScanDialog(self, folder).exec()
        self.refresh_tree()

    def use_contact_photos(self) -> bool:
        return self.settings.value("use_contact_photos", False, type=bool)

    def open_settings(self):
        SettingsDialog(self).exec()

    def check_exiftool(self):
        """Au démarrage : ExifTool est nécessaire pour écrire les noms dans les photos."""
        if write.find_exiftool() or self.settings.value("exiftool_never_ask", False, type=bool):
            return
        ExifToolDialog(self).exec()

    def open_write(self):
        WriteDialog(self).exec()
        self.refresh_tree()

    def closeEvent(self, e):
        self.learn_timer.stop()
        if self.learn_worker and self.learn_worker.isRunning():
            self.learn_worker.wait(30000)
        self.pool.clear()
        self.conn.close()
        super().closeEvent(e)


class SettingsDialog(QDialog):
    """Paramètres : connexion Google Contacts, ExifTool, dossier de photos.

    Chaque utilisateur crée SON propre identifiant OAuth (gratuit) : aucun identifiant n'est livré
    avec le programme, ce qui permet de le partager en open source.
    """

    SIMPLE = (
        "<b>Méthode simple : importer un fichier</b> — aucun compte développeur.<br>"
        "Sur <a href='https://contacts.google.com/'>contacts.google.com</a> : <i>Exporter</i> → "
        "<b>vCard</b> (ou Google CSV), puis <b>Importer un fichier de contacts…</b> ci-dessous.<br>"
        "<small>C'est une copie : refaites un export pour ajouter de nouveaux contacts. Créer des "
        "contacts ou changer leur photo depuis FaceTag nécessite la connexion directe.</small>")

    GUIDE = (
        "<b>Connexion directe (avancé)</b> — créez votre identifiant Google (une fois, ~5 min) :<ol>"
        "<li>Ouvrez <a href='https://console.cloud.google.com/'>Google Cloud Console</a> et créez un projet.</li>"
        "<li><i>API et services → Bibliothèque</i> : activez <b>People API</b>.</li>"
        "<li><i>Écran de consentement OAuth</i> (ou <i>Google Auth Platform → Audience</i>) : type "
        "<b>Externe</b>, puis ajoutez votre adresse Gmail comme <b>utilisateur test</b>.</li>"
        "<li><i>Identifiants → Créer des identifiants → ID client OAuth</i> : type "
        "<b>Application de bureau</b>, puis téléchargez le fichier JSON.</li>"
        "<li>Ici : <b>Importer le fichier JSON…</b>, puis <b>Se connecter</b>.</li></ol>"
        "<small>Le fichier reste sur cet ordinateur (dossier de données de FaceTag), jamais dans le "
        "dossier du programme. En mode « Test », Google redemande l'autorisation après 7 jours.</small>")

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Paramètres")
        self.resize(760, 880)
        self.worker = None
        lay = QVBoxLayout(self)

        # --- Google Contacts
        g = QGroupBox("Google Contacts")
        gl = QVBoxLayout(g)
        simple = QLabel(self.SIMPLE)
        simple.setWordWrap(True)
        simple.setOpenExternalLinks(True)
        gl.addWidget(simple)
        row0 = QHBoxLayout()
        self.b_file = QPushButton("Importer un fichier de contacts (.vcf / .csv)…")
        row0.addWidget(self.b_file)
        row0.addStretch()
        gl.addLayout(row0)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        gl.addWidget(line)
        self.guide = QLabel(self.GUIDE)
        self.guide.setWordWrap(True)
        self.guide.setOpenExternalLinks(True)
        gl.addWidget(self.guide)
        self.g_status = QLabel()
        self.g_status.setWordWrap(True)
        gl.addWidget(self.g_status)
        row1 = QHBoxLayout()
        self.b_import = QPushButton("Importer le fichier JSON…")
        self.b_ids = QPushButton("Saisir l'ID client…")
        self.b_forget = QPushButton("Oublier l'identifiant")
        for b in (self.b_import, self.b_ids, self.b_forget):
            row1.addWidget(b)
        row1.addStretch()
        gl.addLayout(row1)
        row2 = QHBoxLayout()
        self.b_login = QPushButton("Se connecter")
        self.b_login_w = QPushButton("Autoriser aussi l'écriture")
        self.b_login_w.setToolTip("Nécessaire pour créer des contacts et changer leur photo")
        self.b_test = QPushButton("Tester")
        self.b_sync = QPushButton("Importer / mettre à jour les contacts")
        self.b_logout = QPushButton("Se déconnecter")
        for b in (self.b_login, self.b_login_w, self.b_test, self.b_sync, self.b_logout):
            row2.addWidget(b)
        row2.addStretch()
        gl.addLayout(row2)
        lay.addWidget(g)

        # --- Reconnaissance
        rg = QGroupBox("Reconnaissance des visages")
        rl = QVBoxLayout(rg)
        self.chk_contacts = QCheckBox("Utiliser les photos de profil des contacts comme point de départ")
        self.chk_contacts.setChecked(parent.use_contact_photos())
        rl.addWidget(self.chk_contacts)
        expl = QLabel(
            "<small><b>Désactivé (recommandé)</b> : FaceTag ne reconnaît une personne qu'à partir des "
            "visages que <i>vous</i> avez nommés dans vos photos. Il faut nommer quelques visages au "
            "début, mais les suggestions sont fiables.<br>"
            "<b>Activé</b> : la photo de profil d'un contact sert à proposer son nom avant même que "
            "vous ayez nommé un de ses visages. Pratique pour démarrer, mais ces photos sont souvent "
            "petites (~96 px), anciennes, ou ne montrent pas de visage : attendez-vous à plus "
            "d'erreurs. Dès qu'une personne a un visage confirmé, sa photo de profil n'est plus "
            "utilisée.</small>")
        expl.setWordWrap(True)
        rl.addWidget(expl)
        lay.addWidget(rg)

        # --- ExifTool
        e = QGroupBox("ExifTool (écriture des noms dans les photos)")
        el = QHBoxLayout(e)
        self.exif = QLineEdit(config.get("exiftool_path") or "")
        self.exif.setPlaceholderText(write.find_exiftool() or "Chemin de exiftool.exe (voir README)")
        b_browse = QPushButton("Parcourir…")
        b_exif_test = QPushButton("Tester")
        b_exif_install = QPushButton("Installer…")
        b_exif_install.setToolTip("Télécharger et installer ExifTool automatiquement (Windows)")
        el.addWidget(self.exif, 1)
        el.addWidget(b_browse)
        el.addWidget(b_exif_test)
        el.addWidget(b_exif_install)
        b_exif_install.clicked.connect(self._install_exif)
        lay.addWidget(e)

        # --- Photos
        p = QGroupBox("Photos")
        pl = QHBoxLayout(p)
        self.folder = QLineEdit(parent.settings.value("last_folder", ""))
        self.folder.setPlaceholderText("Dossier proposé par défaut pour « Détecter les visages »")
        b_folder = QPushButton("Parcourir…")
        pl.addWidget(self.folder, 1)
        pl.addWidget(b_folder)
        lay.addWidget(p)

        self.log = QLabel()
        self.log.setWordWrap(True)
        self.log.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.log)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Close).setText("Fermer")
        btns.rejected.connect(self.reject)
        lay.addStretch()
        lay.addWidget(btns)

        self.b_import.clicked.connect(self._import)
        self.b_file.clicked.connect(self._import_contacts_file)
        self.chk_contacts.toggled.connect(self._toggle_contact_photos)
        self.b_ids.clicked.connect(self._ids)
        self.b_forget.clicked.connect(self._forget)
        self.b_login.clicked.connect(lambda: self._run("Connexion (voir le navigateur)…",
                                                       lambda progress: contacts.sign_in(False)))
        self.b_login_w.clicked.connect(lambda: self._run("Autorisation d'écriture (voir le navigateur)…",
                                                         lambda progress: contacts.sign_in(True)))
        self.b_test.clicked.connect(lambda: self._run("Test de la connexion…",
                                                      lambda progress: contacts.test_connection(),
                                                      lambda n: f"Connexion OK : {n} contact(s) accessibles."))
        self.b_sync.clicked.connect(lambda: self._run("Import des contacts…", self._sync_job,
                                                      lambda r: r))
        self.b_logout.clicked.connect(self._logout)
        b_browse.clicked.connect(self._browse_exif)
        b_exif_test.clicked.connect(self._test_exif)
        b_folder.clicked.connect(self._browse_folder)
        self.refresh()

    # ------------------------------------------------------------- état
    def refresh(self):
        info = contacts.client_info()
        st = contacts.connection_status()
        if not info:
            txt = "Connexion directe : <b>aucun identifiant configuré</b> (facultatif)."
        else:
            access = ("lecture et écriture" if st["write"] else "lecture seule") if st["connected"] \
                else "<b>non connecté</b>"
            who = "fourni avec FaceTag" if info.get("bundled") else \
                f"projet <b>{info['project']}</b> · client <code>{info['client_id']}</code>"
            txt = f"Identifiant : {who}<br>Connexion : {access}"
        self.guide.setVisible(not (info and info.get("bundled")))
        self.b_forget.setVisible(not (info and info.get("bundled")))
        self.g_status.setText(txt)
        busy = bool(self.worker and self.worker.isRunning())
        for b in (self.b_login, self.b_login_w, self.b_test, self.b_sync, self.b_logout, self.b_forget):
            b.setEnabled(bool(info) and not busy)
        self.b_login.setVisible(not st["connected"])
        self.b_login_w.setVisible(st["connected"] and not st["write"])
        self.b_test.setEnabled(st["connected"] and not busy)
        self.b_logout.setEnabled(st["connected"] and not busy)

    def _say(self, text, error=False):
        self.log.setText(f"<span style='color:#c5221f'>{text}</span>" if error else text)

    def _run(self, message, job, ok_text=None):
        if self.worker and self.worker.isRunning():
            return
        self._say(message)
        self.worker = Worker(job)

        def done(res, err):
            if err:
                self._say(err, error=True)
            else:
                self._say(ok_text(res) if ok_text else "OK")
            self.refresh()
            self.parent().refresh_names()
            self.parent().refresh_tree(reload=False)
        self.worker.done.connect(done)
        self.worker.start()
        self.refresh()

    @staticmethod
    def _sync_job(progress):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            contacts.sync()
        lines = [l for l in out.getvalue().splitlines() if l.startswith("Contacts lus")]
        return lines[-1] if lines else "Import terminé."

    # ------------------------------------------------------------- actions
    def _import_contacts_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Fichier de contacts exporté", "",
                                              "Contacts (*.vcf *.csv)")
        if path:
            self._run("Import du fichier de contacts…",
                      lambda progress: contacts_file.import_file(path, log=lambda s: None),
                      lambda r: r["message"])

    def _toggle_contact_photos(self, on):
        self.parent().settings.setValue("use_contact_photos", on)
        if on:  # créer les visages de référence à partir des photos de profil, puis réapprendre
            def job(progress):
                import contextlib
                import io
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    scan.scan_contacts()
                return out.getvalue().strip().splitlines()[-1] if out.getvalue().strip() else "OK"
            self._run("Analyse des photos de profil…", job, lambda r: r)
        self.parent().learn_timer.start()

    def _install_exif(self):
        ExifToolDialog(self.parent()).exec()
        self.exif.setText(config.get("exiftool_path") or "")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Fichier JSON de l'ID client OAuth", "",
                                              "JSON (*.json)")
        if not path:
            return
        try:
            contacts.import_client_file(path)
        except ValueError as exc:
            self._say(str(exc), error=True)
            return
        self._say("Identifiant importé. Cliquez sur « Se connecter ».")
        self.refresh()

    def _ids(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("ID client OAuth")
        form = QFormLayout(dlg)
        cid, sec = QLineEdit(), QLineEdit()
        sec.setEchoMode(QLineEdit.Password)
        form.addRow("ID client", cid)
        form.addRow("Code secret du client", sec)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            contacts.save_client_ids(cid.text(), sec.text())
        except ValueError as exc:
            self._say(str(exc), error=True)
            return
        self._say("Identifiant enregistré. Cliquez sur « Se connecter ».")
        self.refresh()

    def _logout(self):
        contacts.sign_out()
        self._say("Déconnecté : autorisation révoquée chez Google et jeton local supprimé. "
                  "Vos visages et noms restent dans FaceTag.")
        self.refresh()

    def _forget(self):
        if QMessageBox.question(self, "Oublier l'identifiant",
                                "Supprimer l'identifiant Google de cet ordinateur et se déconnecter ?\n"
                                "Vos visages et noms restent dans FaceTag.") != QMessageBox.Yes:
            return
        contacts.remove_client()
        self._say("Identifiant supprimé.")
        self.refresh()

    def _browse_exif(self):
        path, _ = QFileDialog.getOpenFileName(self, "exiftool.exe", "", "ExifTool (exiftool*.exe exiftool)")
        if path:
            self.exif.setText(os.path.normpath(path))
            config.set("exiftool_path", self.exif.text())
            self._test_exif()

    def _test_exif(self):
        path = self.exif.text().strip() or None
        config.set("exiftool_path", path)
        try:
            self._say(f"ExifTool OK : version {write.exiftool_version(path)}")
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self._say(f"ExifTool : {exc}", error=True)

    def _browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Dossier de photos", self.folder.text())
        if folder:
            self.folder.setText(os.path.normpath(folder))
            self.parent().settings.setValue("last_folder", self.folder.text())

    def reject(self):
        if self.worker and self.worker.isRunning():
            self._say("Une opération est en cours (navigateur ouvert ?) : terminez-la d'abord.", error=True)
            return
        self.parent().settings.setValue("last_folder", self.folder.text().strip())
        super().reject()


class ExifToolDialog(QDialog):
    """ExifTool manquant : proposer de le télécharger et l'installer (Windows), ou de le choisir."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("ExifTool")
        self.resize(600, 300)
        self.mw = parent
        self.worker = None
        lay = QVBoxLayout(self)
        found = write.find_exiftool()
        intro = ("ExifTool est déjà installé. Vous pouvez installer la dernière version." if found else
                 "<b>ExifTool n'est pas installé.</b><br>Il est nécessaire pour écrire les noms des "
                 "personnes dans vos photos (le reste de FaceTag fonctionne sans).")
        txt = QLabel(intro + "<br><br><small>ExifTool est un logiciel libre de Phil Harvey "
                     "(<a href='https://exiftool.org/'>exiftool.org</a>). Le téléchargement officiel "
                     "(~11 Mo) est vérifié par son empreinte SHA-256 publiée sur exiftool.org, puis "
                     "installé dans le dossier de données de FaceTag (sans droits administrateur).</small>")
        txt.setWordWrap(True)
        txt.setOpenExternalLinks(True)
        lay.addWidget(txt)
        self.log = QLabel()
        self.log.setWordWrap(True)
        lay.addWidget(self.log)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        self.never = QCheckBox("Ne plus me le demander au démarrage")
        self.never.setVisible(not found)
        lay.addWidget(self.never)
        lay.addStretch()
        self.btns = QDialogButtonBox()
        self.b_install = self.btns.addButton("Télécharger et installer", QDialogButtonBox.ActionRole)
        self.b_pick = self.btns.addButton("Choisir exiftool.exe…", QDialogButtonBox.ActionRole)
        self.b_later = self.btns.addButton("Plus tard", QDialogButtonBox.RejectRole)
        if not exiftool_setup.supported():
            self.b_install.setEnabled(False)
            self.log.setText(exiftool_setup.manual_help())
        self.b_install.clicked.connect(self._install)
        self.b_pick.clicked.connect(self._pick)
        self.btns.rejected.connect(self.reject)
        lay.addWidget(self.btns)

    def _install(self):
        self.b_install.setEnabled(False)
        self.b_pick.setEnabled(False)
        self.bar.show()
        self.bar.setMaximum(0)
        self.worker = Worker(exiftool_setup.install, log=lambda s: self.worker.logged.emit(s))
        self.worker.logged.connect(self.log.setText)
        self.worker.progressed.connect(lambda d, t: (self.bar.setMaximum(t), self.bar.setValue(d)))
        self.worker.done.connect(self._done)
        self.worker.start()

    def _done(self, path, err):
        self.bar.hide()
        self.b_pick.setEnabled(True)
        if err:
            self.b_install.setEnabled(True)
            self.log.setText(f"<span style='color:#c5221f'>{err}</span><br>Vous pouvez aussi le "
                             "télécharger vous-même sur exiftool.org puis « Choisir exiftool.exe… ».")
            return
        try:
            ver = write.exiftool_version(path)
            self.log.setText(f"✔ ExifTool {ver} installé et prêt.")
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.log.setText(f"<span style='color:#c5221f'>Installé, mais le test a échoué : {exc}</span>")
        self.b_later.setText("Fermer")

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(self, "exiftool.exe", "", "ExifTool (exiftool*.exe exiftool)")
        if not path:
            return
        try:
            ver = write.exiftool_version(path)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.log.setText(f"<span style='color:#c5221f'>Ce fichier ne répond pas comme ExifTool : {exc}</span>")
            return
        config.set("exiftool_path", os.path.normpath(path))
        self.log.setText(f"✔ ExifTool {ver} sélectionné.")
        self.b_later.setText("Fermer")

    def reject(self):
        if self.worker and self.worker.isRunning():
            return
        if self.never.isChecked():
            self.mw.settings.setValue("exiftool_never_ask", True)
        super().reject()


class ContactDialog(QDialog):
    """Prénom / nom du contact Google à créer (pré-rempli à partir du nom saisi)."""

    def __init__(self, parent, name):
        super().__init__(parent)
        self.setWindowTitle("Nouveau contact Google")
        parts = name.split(" ", 1)
        self.given = QLineEdit(parts[0])
        self.family = QLineEdit(parts[1] if len(parts) > 1 else "")
        form = QFormLayout(self)
        form.addRow(QLabel("Vérifiez le prénom et le nom avant la création dans Google Contacts."))
        form.addRow("Prénom", self.given)
        form.addRow("Nom", self.family)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Créer le contact")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def values(self):
        return self.given.text().strip(), self.family.text().strip()


class ScanDialog(QDialog):
    """Détection des visages dans un dossier, puis recalcul des suggestions."""

    def __init__(self, parent, folder):
        super().__init__(parent)
        self.setWindowTitle("Détecter les visages")
        self.resize(760, 420)
        self.stop = threading.Event()
        self.log = QPlainTextEdit(readOnly=True)
        self.bar = QProgressBar()
        self.bar.setMaximum(0)  # animation tant que l'inventaire tourne
        self.btns = QDialogButtonBox()
        self.b_stop = self.btns.addButton("Arrêter", QDialogButtonBox.ActionRole)
        self.b_close = self.btns.addButton("Fermer", QDialogButtonBox.RejectRole)
        self.b_close.setEnabled(False)
        self.b_stop.clicked.connect(self._ask_stop)
        self.btns.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{folder}</b><br>Seules les photos nouvelles ou modifiées sont analysées. "
                             "Vous pouvez arrêter à tout moment : la progression est conservée."))
        lay.addWidget(self.log, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.btns)

        use = parent.use_contact_photos()

        def job(progress):
            res = scan.scan(folder, log=self.worker.logged.emit, progress=progress, stop=self.stop)
            if res.get("done") and not self.stop.is_set():
                self.worker.logged.emit("Calcul des suggestions…")
                self.worker.logged.emit(match.run(use_contact_photos=use))
            return res

        self.worker = Worker(job)
        self.worker.logged.connect(self.log.appendPlainText)
        self.worker.progressed.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.start()

    def _progress(self, done, total):
        self.bar.setMaximum(total)
        self.bar.setValue(done)

    def _ask_stop(self):
        self.stop.set()
        self.b_stop.setEnabled(False)
        self.log.appendPlainText("Arrêt demandé…")

    def _done(self, res, err):
        if err:
            self.log.appendPlainText("ERREUR : " + err)
        self.bar.setMaximum(1)
        self.bar.setValue(1)
        self.b_stop.setEnabled(False)
        self.b_close.setEnabled(True)

    def reject(self):
        if self.worker.isRunning():
            self._ask_stop()
            return  # fermeture possible une fois l'arrêt effectif
        super().reject()


class WriteDialog(QDialog):
    """Simulation d'abord, puis écriture réelle sur confirmation."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Écrire les noms dans les photos")
        self.resize(760, 420)
        self.log = QPlainTextEdit(readOnly=True)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Optionnel : limiter à un dossier, ex. \\\\NAS\\photo\\2024")
        self.bar = QProgressBar()
        self.btns = QDialogButtonBox()
        self.b_sim = self.btns.addButton("Simuler", QDialogButtonBox.ActionRole)
        self.b_apply = self.btns.addButton("Écrire dans les photos", QDialogButtonBox.ActionRole)
        self.btns.addButton("Fermer", QDialogButtonBox.RejectRole)
        self.b_apply.setEnabled(False)
        self.b_sim.clicked.connect(lambda: self._go(False))
        self.b_apply.clicked.connect(lambda: self._go(True))
        self.btns.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Les zones de visage et les noms confirmés sont écrits dans les photos "
                             "(XMP, norme MWG).<br>Conseil : testez d'abord sur un dossier de copies."))
        lay.addWidget(self.folder)
        lay.addWidget(self.log, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.btns)
        self.worker = None
        self._go(False)

    def _go(self, apply):
        if self.worker and self.worker.isRunning():
            return
        self.b_sim.setEnabled(False)
        self.b_apply.setEnabled(False)
        folder = self.folder.text().strip() or None
        self.worker = Worker(write.run, apply=apply, folder=folder,
                             log=lambda s: self.worker.logged.emit(s))
        self.worker.logged.connect(self.log.appendPlainText)
        self.worker.progressed.connect(lambda d, t: (self.bar.setMaximum(max(t, 1)), self.bar.setValue(d)))
        self.worker.done.connect(lambda res, err: self._done(apply, res, err))
        self.worker.start()

    def _done(self, apply, res, err):
        self.b_sim.setEnabled(True)
        if err:
            self.log.appendPlainText("ERREUR : " + err)
            return
        self.b_apply.setEnabled(not apply and res and res["photos"] > 0)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
