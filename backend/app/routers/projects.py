from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ProcessingJob, Project, Video
from ..schemas import (
    JobOut,
    ProjectCreate,
    ProjectOut,
    ProjectSummary,
    ProjectUpdate,
    VideoOut,
)
from ..services.storage import storage

router = APIRouter(prefix="/projects", tags=["projects"])


def video_to_out(video: Video) -> VideoOut:
    payload = VideoOut.model_validate(video)
    payload.stream_url = f"/api/v1/videos/{video.id}/stream"
    return payload


def project_to_out(project: Project, db: Session) -> ProjectOut:
    payload = ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        source_language=project.source_language,
        target_languages=project.target_languages,
        status=project.status,
        created_at=project.created_at,
        updated_at=project.updated_at,
        videos=[],
    )
    videos = db.scalars(
        select(Video).where(Video.project_id == project.id).order_by(Video.created_at.desc())
    ).all()
    payload.videos = [video_to_out(video) for video in videos]
    return payload


@router.get("", response_model=list[ProjectSummary])
def list_projects(
    search: str | None = Query(default=None, max_length=120),
    db: Session = Depends(get_db),
) -> list[ProjectSummary]:
    query = select(Project)
    if search:
        query = query.where(Project.name.ilike(f"%{search}%"))
    projects = db.scalars(query.order_by(Project.updated_at.desc())).all()
    result: list[ProjectSummary] = []
    for project in projects:
        count = db.scalar(select(func.count(Video.id)).where(Video.project_id == project.id)) or 0
        latest = db.scalars(
            select(ProcessingJob)
            .where(ProcessingJob.project_id == project.id)
            .order_by(ProcessingJob.created_at.desc())
        ).first()
        item = ProjectSummary.model_validate(project)
        item.video_count = count
        item.latest_job_status = latest.status if latest else None
        result.append(item)
    return result


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    project = Project(
        name=payload.name.strip(),
        description=payload.description,
        source_language=payload.source_language,
        target_languages=payload.target_languages,
        status="draft",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    storage.project_dir(project.id)
    return project_to_out(project, db)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return project_to_out(project, db)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)) -> ProjectOut:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"]:
        project.name = changes["name"].strip()
    if "description" in changes:
        project.description = changes["description"]
    if "status" in changes and changes["status"]:
        project.status = changes["status"]
    db.commit()
    db.refresh(project)
    return project_to_out(project, db)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: Session = Depends(get_db)) -> Response:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    db.delete(project)
    db.commit()
    storage.delete_project(project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/jobs", response_model=list[JobOut])
def list_project_jobs(project_id: str, db: Session = Depends(get_db)) -> list[JobOut]:
    if not db.get(Project, project_id):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    jobs = db.scalars(
        select(ProcessingJob).where(ProcessingJob.project_id == project_id).order_by(ProcessingJob.created_at.desc())
    ).all()
    return [JobOut.model_validate(job) for job in jobs]
