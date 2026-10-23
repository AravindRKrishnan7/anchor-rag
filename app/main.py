from fastapi import FastAPI

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
    return PEOPLE[person_id]