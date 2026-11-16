package farm.api;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import farm.sim.SimServiceClient;
import farm.sim.SimServiceClient.SimResponse;
import farm.sim.SimServiceClient.SimUnavailableException;

/**
 * Public API for the dashboard. Every route maps to one simulator-service endpoint; the
 * simulator validates all dispatches and events, so this layer only forwards and reports.
 */
@RestController
@RequestMapping(value = "/api", produces = MediaType.APPLICATION_JSON_VALUE)
public class FarmController {

    private static final Set<String> MEMORY_FILTERS = Set.of("tractor_id", "event_type", "operation", "scenario");
    private static final Set<String> EPISODE_FILTERS = Set.of("scenario_id", "policy", "q", "limit");
    private static final Pattern SAFE_ID = Pattern.compile("[A-Za-z0-9._-]{1,80}");

    private final SimServiceClient sim;

    public FarmController(SimServiceClient sim) {
        this.sim = sim;
    }

    /** Backend health plus whether the simulator service answers. */
    @GetMapping("/health")
    public ResponseEntity<String> health() {
        try {
            SimResponse r = sim.get("/health");
            return json(HttpStatus.OK, "{\"backend\":\"ok\",\"simulator\":" + r.body() + "}");
        } catch (SimUnavailableException e) {
            return json(HttpStatus.OK, "{\"backend\":\"ok\",\"simulator\":{\"ok\":false,\"url\":\""
                    + sim.baseUrl() + "\"}}");
        }
    }

    @GetMapping("/state")
    public ResponseEntity<String> state() { return relay(sim.get("/state")); }

    @PostMapping("/reset")
    public ResponseEntity<String> reset(@RequestBody(required = false) String body) { return relay(sim.post("/reset", body)); }

    @PostMapping("/step")
    public ResponseEntity<String> step(@RequestBody(required = false) String body) { return relay(sim.post("/step", body)); }

    /** Operator events: breakdown, repair, block_cell, unblock_cell, change_priority, refuel. */
    @PostMapping("/events")
    public ResponseEntity<String> event(@RequestBody String body) { return relay(sim.post("/events", body)); }

    @PostMapping("/estimate")
    public ResponseEntity<String> estimate(@RequestBody String body) { return relay(sim.post("/estimate", body)); }

    /** Validate (commit=false) or commit (commit=true) a tractor-to-job plan. */
    @PostMapping("/dispatch")
    public ResponseEntity<String> dispatch(@RequestBody String body) { return relay(sim.post("/dispatch", body)); }

    /** FCFS baseline vs the live run on the same scenario, events and horizon. */
    @PostMapping("/compare")
    public ResponseEntity<String> compare() { return relay(sim.post("/compare", "{}")); }

    /** At most five candidate memories matching tractor, event type, operation or scenario. */
    @GetMapping("/memory")
    public ResponseEntity<String> searchMemory(@RequestParam Map<String, String> params) {
        String q = params.entrySet().stream()
                .filter(e -> MEMORY_FILTERS.contains(e.getKey()) && !e.getValue().isBlank())
                .map(e -> e.getKey() + "=" + URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8))
                .collect(Collectors.joining("&"));
        return relay(sim.get("/memory" + (q.isEmpty() ? "" : "?" + q)));
    }

    @GetMapping("/memory/recent")
    public ResponseEntity<String> recentMemory(@RequestParam(defaultValue = "20") int limit) {
        return relay(sim.get("/memory/recent?limit=" + Math.max(1, Math.min(limit, 50))));
    }

    /** Save an operator-written candidate lesson (never a rule). */
    @PostMapping("/memory")
    public ResponseEntity<String> saveMemory(@RequestBody String body) { return relay(sim.post("/memory", body)); }

    // ---------------------------------------------------------------- Simulation Lab

    /** Scenario library (realistic field-condition scenarios + the original demo farm). */
    @GetMapping("/scenarios")
    public ResponseEntity<String> scenarios() { return relay(sim.get("/scenarios")); }

    /** Start a background batch: {"scenario_ids":[], "policies":[], "seeds":[]}. */
    @PostMapping("/lab/run")
    public ResponseEntity<String> labRun(@RequestBody String body) { return relay(sim.post("/lab/run", body)); }

    /** One finished run with its per-tick timeline and map. */
    @GetMapping("/lab/runs/{id}")
    public ResponseEntity<String> labRunDetail(@PathVariable String id) { return relay(sim.get("/lab/runs/" + safeId(id))); }

    @GetMapping("/jobs")
    public ResponseEntity<String> jobs() { return relay(sim.get("/jobs")); }

    @GetMapping("/jobs/{id}")
    public ResponseEntity<String> job(@PathVariable String id) { return relay(sim.get("/jobs/" + safeId(id))); }

    // ---------------------------------------------------------------- Learning memory

    /** Start self-improvement training: {"scenario_ids":[], "rounds": n}. */
    @PostMapping("/learn/train")
    public ResponseEntity<String> train(@RequestBody(required = false) String body) { return relay(sim.post("/learn/train", body)); }

    /** Skills, facts, learning curve and measured insights. */
    @GetMapping("/learning")
    public ResponseEntity<String> learning() { return relay(sim.get("/learning")); }

    @GetMapping("/learning/episodes")
    public ResponseEntity<String> episodes(@RequestParam Map<String, String> params) {
        String q = params.entrySet().stream()
                .filter(e -> EPISODE_FILTERS.contains(e.getKey()) && !e.getValue().isBlank())
                .map(e -> e.getKey() + "=" + URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8))
                .collect(Collectors.joining("&"));
        return relay(sim.get("/learning/episodes" + (q.isEmpty() ? "" : "?" + q)));
    }

    /** A skill's SKILL.md. */
    @GetMapping("/learning/skills/{id}")
    public ResponseEntity<String> skill(@PathVariable String id) { return relay(sim.get("/learning/skills/" + safeId(id))); }

    /** Run the read-only LLM assessment agent (requires HF_TOKEN on the simulator service). */
    @PostMapping("/assess")
    public ResponseEntity<String> assess(@RequestBody(required = false) String body) { return relay(sim.post("/assess", body)); }

    /** Farmer chat: answers from live state; proposes (never applies) operator events. */
    @PostMapping("/chat")
    public ResponseEntity<String> chat(@RequestBody(required = false) String body) { return relay(sim.post("/chat", body)); }

    @ExceptionHandler(SimUnavailableException.class)
    public ResponseEntity<String> unavailable(SimUnavailableException e) {
        return json(HttpStatus.BAD_GATEWAY, SimServiceClient.errorJson(e.getMessage()));
    }

    /** Path ids are forwarded into another URL, so only allow plain identifiers. */
    private static String safeId(String id) {
        if (!SAFE_ID.matcher(id).matches()) {
            throw new IllegalArgumentException("invalid id");
        }
        return id;
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<String> badRequest(IllegalArgumentException e) {
        return json(HttpStatus.BAD_REQUEST, SimServiceClient.errorJson(e.getMessage()));
    }

    private static ResponseEntity<String> relay(SimResponse r) {
        return ResponseEntity.status(r.status()).contentType(MediaType.APPLICATION_JSON).body(r.body());
    }

    private static ResponseEntity<String> json(HttpStatus status, String body) {
        return ResponseEntity.status(status).contentType(MediaType.APPLICATION_JSON).body(body);
    }
}
