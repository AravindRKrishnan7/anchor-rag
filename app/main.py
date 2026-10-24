from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

PEOPLE = {
1: {"name": "Captain Ravi Menon", "role": "Chief Engineer", "services": ["ship maintenance"]},
2: {"name": "Anita Thomas", "role": "Port Consultant", "services": ["port operations"]},
}

@app.get("/people/{person_id}")
def get_person(person_id: int):
    person = PEOPLE.get(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person