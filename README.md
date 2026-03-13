# ClinicOS (Hackathon Demo)
+
Doctor’s Appointment & Scheduling System with:
- Role-based authentication (Receptionist vs Doctor)
- SQLite database persistence
- Automatic clash-prevention for overlapping bookings
- Booking, cancellation, rescheduling flows + reminder (demo) and completion
+
## Run locally (Windows)
+
```powershell
python .\\server.py
```
+
Then open http://127.0.0.1:8000
+
### Demo credentials
- Receptionist: `reception` / `reception123`
- Doctor: `doctor` / `doctor123`
+
## Notes
- Data is stored in `data/clinic.db` (SQLite).
- This is a hackathon-grade demo server (Python stdlib HTTP server).
+
