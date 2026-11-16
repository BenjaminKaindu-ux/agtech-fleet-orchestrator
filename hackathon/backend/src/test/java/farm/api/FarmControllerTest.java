package farm.api;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.http.ResponseEntity;

import com.sun.net.httpserver.HttpServer;

import farm.sim.SimServiceClient;

/** Runs the controller against a fake simulator service on a random local port. */
class FarmControllerTest {

    private static HttpServer fakeSim;
    private static final AtomicReference<String> lastRequest = new AtomicReference<>();
    private static FarmController controller;

    @BeforeAll
    static void start() throws IOException {
        fakeSim = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fakeSim.createContext("/", exchange -> {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            lastRequest.set(exchange.getRequestMethod() + " " + exchange.getRequestURI() + " " + body);
            String path = exchange.getRequestURI().getPath();
            int status = path.equals("/assess") ? 503 : 200;
            byte[] out = (path.equals("/health") ? "{\"ok\":true,\"tick\":3}"
                    : status == 503 ? "{\"error\":\"set HF_TOKEN\"}" : "{\"echo\":\"" + path + "\"}")
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, out.length);
            exchange.getResponseBody().write(out);
            exchange.close();
        });
        fakeSim.start();
        String url = "http://127.0.0.1:" + fakeSim.getAddress().getPort();
        controller = new FarmController(new SimServiceClient(url, Duration.ofSeconds(2), Duration.ofSeconds(5)));
    }

    @AfterAll
    static void stop() {
        fakeSim.stop(0);
    }

    @Test
    void healthIncludesSimulator() {
        ResponseEntity<String> r = controller.health();
        assertEquals(200, r.getStatusCode().value());
        assertTrue(r.getBody().contains("\"simulator\":{\"ok\":true"));
    }

    @Test
    void stepForwardsBody() {
        controller.step("{\"ticks\":5}");
        assertEquals("POST /step {\"ticks\":5}", lastRequest.get());
    }

    @Test
    void memorySearchKeepsOnlyKnownFilters() {
        controller.searchMemory(Map.of("tractor_id", "T2", "evil", "x", "operation", "spot spray"));
        String req = lastRequest.get();
        assertTrue(req.startsWith("GET /memory?"));
        assertTrue(req.contains("tractor_id=T2"));
        assertTrue(req.contains("operation=spot+spray"));
        assertTrue(!req.contains("evil"));
    }

    @Test
    void simulatorErrorStatusIsRelayed() {
        ResponseEntity<String> r = controller.assess("{}");
        assertEquals(503, r.getStatusCode().value());
        assertTrue(r.getBody().contains("HF_TOKEN"));
    }

    @Test
    void labAndLearningRoutesForward() {
        controller.labRun("{\"scenario_ids\":[\"hay-cut-cure-bale\"]}");
        assertEquals("POST /lab/run {\"scenario_ids\":[\"hay-cut-cure-bale\"]}", lastRequest.get());
        controller.skill("spring-planting-rain-v2-ab12");
        assertEquals("GET /learning/skills/spring-planting-rain-v2-ab12 ", lastRequest.get());
        controller.episodes(Map.of("scenario_id", "spray-wind-window", "junk", "x"));
        assertEquals("GET /learning/episodes?scenario_id=spray-wind-window ", lastRequest.get());
    }

    @Test
    void chatForwardsBody() {
        controller.chat("{\"message\":\"what is T2 doing?\"}");
        assertEquals("POST /chat {\"message\":\"what is T2 doing?\"}", lastRequest.get());
    }

    @Test
    void pathIdsAreValidated() {
        org.junit.jupiter.api.Assertions.assertThrows(IllegalArgumentException.class,
                () -> controller.labRunDetail("../state"));
    }

    @Test
    void unreachableSimulatorReportsDown() {
        FarmController offline = new FarmController(
                new SimServiceClient("http://127.0.0.1:1", Duration.ofMillis(300), Duration.ofMillis(300)));
        ResponseEntity<String> r = offline.health();
        assertTrue(r.getBody().contains("\"ok\":false"));
    }
}
