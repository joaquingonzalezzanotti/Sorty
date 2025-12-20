import uuid
import secrets
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, UniqueConstraint, text

db = SQLAlchemy()


def default_code(length: int = 8) -> str:
  token = secrets.token_urlsafe(10)
  cleaned = "".join(ch for ch in token if ch.isalnum())
  return cleaned[:length].upper()


exclusions = db.Table(
  "exclusion",
  db.Column("from_id", db.Integer, db.ForeignKey("participante.id", ondelete="CASCADE"), primary_key=True),
  db.Column("to_id", db.Integer, db.ForeignKey("participante.id", ondelete="CASCADE"), primary_key=True),
  CheckConstraint("from_id <> to_id", name="ck_exclusion_not_self"),
)


class Sorteo(db.Model):
  __tablename__ = "sorteo"

  id = db.Column(db.Integer, primary_key=True)
  public_id = db.Column(db.Uuid, default=uuid.uuid4, nullable=False, unique=True, index=True)
  code = db.Column(db.String(12), default=default_code, nullable=False, unique=True, index=True)

  nombre = db.Column(db.String(255), nullable=False)
  email_admin = db.Column(db.String(320), nullable=False)  # sin encriptar
  estado = db.Column(db.String(20), nullable=False, default="borrador")

  fecha_creacion = db.Column(db.DateTime(timezone=True), server_default=text("now()"), nullable=False)
  fecha_expiracion = db.Column(
    db.DateTime(timezone=True), server_default=text("now() + interval '7 days'"), nullable=False
  )

  participantes = db.relationship(
    "Participante", back_populates="sorteo", cascade="all, delete-orphan", passive_deletes=True
  )
  asignaciones = db.relationship(
    "Asignacion", back_populates="sorteo", cascade="all, delete-orphan", passive_deletes=True
  )

  __table_args__ = (CheckConstraint("estado IN ('borrador','finalizado')", name="ck_draw_estado"),)

  def __repr__(self) -> str:  # pragma: no cover
    return f"<Sorteo id={self.id} code={self.code} estado={self.estado}>"


class Participante(db.Model):
  __tablename__ = "participante"

  id = db.Column(db.Integer, primary_key=True)
  sorteo_id = db.Column(db.Integer, db.ForeignKey("sorteo.id", ondelete="CASCADE"), nullable=False)
  nombre = db.Column(db.String(255), nullable=False)
  email = db.Column(db.String(320), nullable=False)

  sorteo = db.relationship("Sorteo", back_populates="participantes")

  exclusiones = db.relationship(
    "Participante",
    secondary=exclusions,
    primaryjoin=id == exclusions.c.from_id,
    secondaryjoin=id == exclusions.c.to_id,
    backref="excluido_por",
  )

  __table_args__ = (UniqueConstraint("sorteo_id", "email", name="uq_participante_email_sorteo"),)

  def __repr__(self) -> str:  # pragma: no cover
    return f"<Participante id={self.id} nombre={self.nombre}>"


class Asignacion(db.Model):
  __tablename__ = "asignacion"

  id = db.Column(db.Integer, primary_key=True)
  sorteo_id = db.Column(db.Integer, db.ForeignKey("sorteo.id", ondelete="CASCADE"), nullable=False)
  giver_id = db.Column(db.Integer, db.ForeignKey("participante.id", ondelete="CASCADE"), nullable=False)
  receiver_id = db.Column(db.Integer, db.ForeignKey("participante.id", ondelete="CASCADE"), nullable=False)

  sorteo = db.relationship("Sorteo", back_populates="asignaciones")
  giver = db.relationship("Participante", foreign_keys=[giver_id])
  receiver = db.relationship("Participante", foreign_keys=[receiver_id])

  __table_args__ = (
    UniqueConstraint("sorteo_id", "giver_id", name="uq_match_giver_sorteo"),
    UniqueConstraint("sorteo_id", "receiver_id", name="uq_match_receiver_sorteo"),
    CheckConstraint("giver_id <> receiver_id", name="ck_match_not_self"),
  )

  def __repr__(self) -> str:  # pragma: no cover
    return f"<Asignacion sorteo={self.sorteo_id} {self.giver_id}->{self.receiver_id}>"
