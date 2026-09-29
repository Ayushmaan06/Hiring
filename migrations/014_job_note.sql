-- A line of progress per job (IMPLEMENTATION.md 1.11d step 4).
--
-- Reading a LinkedIn profile takes ~25s, so a button that starts eight of them runs for
-- minutes. A spinner over that is a lie: it looks identical whether the run is on its
-- third profile or was blocked on its first. The worker writes what it is doing here and
-- the page polls it.

alter table job add column note text;
