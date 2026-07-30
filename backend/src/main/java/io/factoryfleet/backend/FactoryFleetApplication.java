package io.factoryfleet.backend;

import java.time.Clock;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;

@SpringBootApplication
public class FactoryFleetApplication {

    public static void main(String[] args) {
        SpringApplication.run(FactoryFleetApplication.class, args);
    }

    /** Injected wherever timestamps are recorded, so tests can pin "now". */
    @Bean
    Clock clock() {
        return Clock.systemUTC();
    }
}
